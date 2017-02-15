#!/usr/bin/env python
import rospy
import json
import yaml
from os.path import join
from rospkg import RosPack
from nips2016.controller import Perception, Learning, Torso, Ergo
from trajectory_msgs.msg import JointTrajectory
from copy import deepcopy


class Controller(object):
    def __init__(self):
        self.rospack = RosPack()
        with open(join(self.rospack.get_path('nips2016'), 'config', 'general.json')) as f:
            self.params = json.load(f)
        self.experiment = rospy.get_param('/nips2016/experiment')
        self.torso = Torso()
        self.ergo = Ergo()
        self.learning = Learning()
        self.perception = Perception()
        rospy.loginfo('Controller fully started!')

    def run(self):
        start_condition_name = self.experiment['done']['condition']
        start_condition_id = self.experiment['sequence'].index(start_condition_name)
        start_trial = self.experiment['done']['trial']
        start_iteration = self.experiment['done']['iteration']
        for condition_id in range(start_condition_id, len(self.experiment['sequence'])):
            condition = self.experiment['sequence'][condition_id]
            params = self.experiment['params'][condition]
            for trial in range(start_trial, params['num_trials']):
                for iteration in range(start_iteration, params['num_iterations']):
                    try:
                        if rospy.is_shutdown():
                            return

                        if iteration % self.params['ergo_reset'] == 1:
                            self.ergo.reset(True)

                        self.experiment['current'] = {'condition': condition, 'iteration': iteration, 'trial': trial}
                        rospy.set_param('/nips2016/experiment', self.experiment)
                        self.execute_iteration(iteration, trial, params)
                    finally:
                        self.experiment['done']['condition'] = condition
                        self.experiment['done']['trial'] = trial
                        self.experiment['done']['iteration'] = iteration
                        rospy.set_param('/nips2016/experiment', self.experiment)
                        dump_exp = deepcopy(self.experiment)
                        del dump_exp['current']
                        with open(join(self.rospack.get_path('nips2016'), 'config', 'experiment.yaml'), 'w') as f:
                            yaml.dump(dump_exp, f)

    def execute_iteration(self, iteration, trial, params):
        rospy.logwarn("Controller starts iteration {}/{} trial {}/{}".format(iteration+1, params['num_iterations'],
                                                                trial+1, params['num_trials']))
        if self.perception.help_pressed():
            rospy.sleep(1.5)  # Wait for the robot to fully stop
            recording = self.perception.record(human_demo=True, nb_points=self.params['nb_points'])
            self.torso.reset(slow=True)
        else:
            trajectory = self.learning.produce().torso_trajectory
            self.torso.execute_trajectory(trajectory)  # TODO: blocking, non-blocking, action server?
            recording = self.perception.record(human_demo=False, nb_points=self.params['nb_points'])
            recording.demo.torso_demonstration = JointTrajectory()
            self.torso.reset(slow=False)
        self.learning.perceive(recording.demo)  # TODO non-blocking

rospy.init_node("nips2016_controller")
Controller().run()
