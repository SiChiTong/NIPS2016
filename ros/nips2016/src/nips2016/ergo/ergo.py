import rospy
import rosnode
import json
from nips2016.srv import *
from nips2016.msg import *
from geometry_msgs.msg import PoseStamped
from sensor_msgs.msg import Joy
from std_msgs.msg import Bool
from poppy.creatures import PoppyErgoJr
from rospkg import RosPack
from os.path import join
from .button import Button


class Ergo(object):
    def __init__(self):
        self.rospack = RosPack()
        with open(join(self.rospack.get_path('nips2016'), 'config', 'ergo.json')) as f:
            self.params = json.load(f)
        self.button = Button(self.params)
        self.rate = rospy.Rate(self.params['publish_rate'])
        self.eef_pub = rospy.Publisher('/nips2016/ergo/end_effector_pose', PoseStamped, queue_size=1)
        self.state_pub = rospy.Publisher('/nips2016/ergo/state', CircularState, queue_size=1)
        self.button_pub = rospy.Publisher('/nips2016/ergo/button', Bool, queue_size=1)

        self.joy1_x = 0.
        self.joy1_y = 0.
        self.joy2_x = 0.
        self.joy2_y = 0.
        rospy.Subscriber('/nips2016/sensors/joystick/1', Joy, self.cb_joy_1)
        rospy.Subscriber('/nips2016/sensors/joystick/2', Joy, self.cb_joy_2)

        self.srv_reset = None
        self.ergo = None
        self.extended = False
        self.standby = False
        self.last_activity = rospy.Time.now()

    def cb_joy_1(self, msg):
        self.joy1_x = msg.axes[0]
        self.joy1_y = msg.axes[1]

    def cb_joy_2(self, msg):
        self.joy2_x = msg.axes[0]
        self.joy2_y = msg.axes[1]

    def go_to_start(self, slow=True):
        self.go_to([0.0, -15.4, 35.34, -8.06, -15.69, 71.99], 4 if slow else 1)

    def go_to_extended(self):
        extended = {'m2': 60, 'm3': -37, 'm5': -50, 'm6': 96}
        self.ergo.goto_position(extended, 0.5)
        self.extended = True

    def go_to_rest(self):
        rest = {'m2': -26, 'm3': 59, 'm5': -30, 'm6': 78}
        self.ergo.goto_position(rest, 0.5)
        self.extended = False

    def is_controller_running(self):
        return len([node for node in rosnode.get_node_names() if 'controller' in node]) > 0

    def go_or_resume_standby(self):
        recent_activity = rospy.Time.now() - self.last_activity < rospy.Duration(self.params['auto_standby_duration'])
        if recent_activity and self.standby:
            rospy.loginfo("Ergo is resuming from standby")
            self.ergo.compliant = False
            self.standby = False
        elif not self.standby and not recent_activity:
            rospy.loginfo("Ergo is entering standby mode")
            self.standby = True
            self.ergo.compliant = True

        if self.is_controller_running():
            self.last_activity = rospy.Time.now()

    def go_to(self, motors, duration):
        self.ergo.goto_position(dict(zip(['m1', 'm2', 'm3', 'm4', 'm5', 'm6'], motors)), duration)
        rospy.sleep(duration)

    def run(self, dummy=False):
        try:
            self.ergo = PoppyErgoJr(use_http=True, simulator='poppy-simu' if dummy else None, camera='dummy')
        except IOError as e:
            rospy.logerr("Ergo hardware failed to init: {}".format(e))
            return None

        self.ergo.compliant = False
        self.go_to_start()
        self.last_activity = rospy.Time.now()
        self.srv_reset = rospy.Service('/nips2016/ergo/reset', Reset, self._cb_reset)
        rospy.loginfo('Ergo is ready and starts joystick servoing...')

        while not rospy.is_shutdown():
            self.go_or_resume_standby()
            self.servo_robot(self.joy1_y, self.joy1_x)
            self.publish_eef()
            self.publish_state()
            self.publish_button()

            # Update the last activity
            if abs(self.joy1_x) > self.params['min_joy_activity'] or abs(self.joy1_y) > self.params['min_joy_activity']:
                self.last_activity = rospy.Time.now()

            self.rate.sleep()

        self.ergo.compliant = True
        self.ergo.close()

    def servo_axis_rotation(self, x):
        x = x if abs(x) > self.params['sensitivity_joy'] else 0
        p = self.ergo.motors[0].goal_position
        min_x = self.params['bounds'][0][0] + self.params['bounds'][3][0]
        max_x = self.params['bounds'][0][1] + self.params['bounds'][3][1]
        new_x = min(max(min_x, p + self.params['speed']*x/self.params['publish_rate']), max_x)
        if new_x > self.params['bounds'][0][1]:
            new_x_m3 = new_x - self.params['bounds'][0][1]
        elif new_x < self.params['bounds'][0][0]:
            new_x_m3 = new_x - self.params['bounds'][0][0]
        else:
            new_x_m3 = 0
        new_x_m3 = max(min(new_x_m3, self.params['bounds'][3][1]), self.params['bounds'][3][0])
        self.ergo.motors[0].goto_position(new_x, 1.1/self.params['publish_rate'])
        self.ergo.motors[3].goto_position(new_x_m3, 1.1/self.params['publish_rate'])

    def servo_axis_elongation(self, x):
        if x > self.params['min_joy_elongation']:
            self.go_to_extended()
        else:
            self.go_to_rest()

    def servo_axis(self, x, id):
        x = x if abs(x) > self.params['sensitivity_joy'] else 0
        p = self.ergo.motors[id].goal_position
        new_x = p + self.params['speed']*x/self.params['publish_rate']
        if self.params['bounds'][id][0] < new_x < self.params['bounds'][id][1]:
            self.ergo.motors[id].goto_position(new_x, 1.1/self.params['publish_rate'])

    def servo_robot(self, x, y):
        if self.params['control_joystick_id'] == 2:
            self.servo_axis_rotation(-x)
            self.servo_axis_elongation(y)
        else:
            self.servo_axis_rotation(y)
            self.servo_axis_elongation(x)


    def publish_eef(self):
        pose = PoseStamped()
        pose.header.frame_id = 'ergo_base'
        eef_pose = self.ergo.chain.end_effector
        pose.header.stamp = rospy.Time.now()
        pose.pose.position.x = eef_pose[0]
        pose.pose.position.y = eef_pose[1]
        pose.pose.position.z = eef_pose[2]
        self.eef_pub.publish(pose)

    def publish_button(self):
        self.button_pub.publish(Bool(data=self.button.pressed))

    def publish_state(self):
        # TODO We might want a better state here, get the arena center, get EEF and do the maths as in environment/get_state
        angle = self.ergo.motors[0].present_position + self.ergo.motors[3].present_position
        self.state_pub.publish(CircularState(angle=angle, extended=self.extended))

    def _cb_reset(self, request):
        rospy.loginfo("Resetting Ergo...")
        self.go_to_start(request.slow)
        return ResetResponse()

