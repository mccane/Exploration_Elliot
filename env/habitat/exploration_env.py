import math
import os
import pickle
import sys
import pdb

import gym
import matplotlib
import numpy as np
import quaternion
import skimage.morphology
import torch
from PIL import Image
from torch.nn import functional as F
from torchvision import transforms
from env.utils.BFS import bfs
from scipy.io import savemat
from pytictoc import TicToc


if sys.platform == 'darwin':
    matplotlib.use("tkagg")
else:
    matplotlib.use('Agg')
import matplotlib.pyplot as plt

plt.ion()
#fig, ax = plt.subplots()

import habitat
from habitat import logger

from env.utils.map_builder import MapBuilder
from env.utils.fmm_planner import FMMPlanner

from env.habitat.utils.noisy_actions import CustomActionSpaceConfiguration

from env.habitat.utils import pose as pu
from env.habitat.utils import visualizations as vu


from env.habitat.utils.supervision import HabitatMaps

from habitat.sims.habitat_simulator.actions import HabitatSimActions

from model import get_grid

timer = TicToc()


def _preprocess_depth(depth):
    depth = depth[:, :, 0]*1
    mask2 = depth > 0.99
    depth[mask2] = 0.

    for i in range(depth.shape[1]):
        depth[:,i][depth[:,i] == 0.] = depth[:,i].max()

    mask1 = depth == 0
    depth[mask1] = np.NaN
    depth = depth*1000.
    return depth


class Exploration_Env(habitat.RLEnv):

    def __init__(self, args, rank, config_env, config_baseline, dataset):
        if args.visualize:
            plt.ion()
            
        if args.print_images or args.visualize:
            self.figure, self.ax = plt.subplots(1,4, figsize=(5, 2), #(20, 10)
                                                facecolor="whitesmoke",
                                                num="Thread {}".format(rank))
        
        #print(config_env)
        self.args = args
        self.num_actions = 3
        self.dt = 10

        self.rank = rank

        self.sensor_noise_fwd = \
                pickle.load(open("noise_models/sensor_noise_fwd.pkl", 'rb'))
        self.sensor_noise_right = \
                pickle.load(open("noise_models/sensor_noise_right.pkl", 'rb'))
        self.sensor_noise_left = \
                pickle.load(open("noise_models/sensor_noise_left.pkl", 'rb'))

        HabitatSimActions.extend_action_space("NOISY_FORWARD")
        HabitatSimActions.extend_action_space("NOISY_RIGHT")
        HabitatSimActions.extend_action_space("NOISY_LEFT")

        #print("hahaha")
        #print(HabitatSimActions.NOISY_FORWARD)

        config_env.defrost()
        config_env.SIMULATOR.ACTION_SPACE_CONFIG = \
                "CustomActionSpaceConfiguration"
        config_env.freeze()

        super().__init__(config_env, dataset)

        self.action_space = gym.spaces.Discrete(self.num_actions)
        #print("hello")
        #print(self.num_actions)

        self.observation_space = gym.spaces.Box(0, 255,
                                                (3, args.frame_height,
                                                    args.frame_width),
                                                dtype='uint8')

        self.mapper = self.build_mapper()

        self.episode_no = 0

        self.res = transforms.Compose([transforms.ToPILImage(),
                    transforms.Resize((args.frame_height, args.frame_width),
                                      interpolation = Image.NEAREST)])
        self.scene_name = None
        self.maps_dict = {}
        self.dump_dir = f"{args.dump_location}/dump/{args.exp_name}/" #"./tmp/dump/exp#/"
        self.local_explore_width = self.args.local_explore_width
        

    def randomize_env(self):
        self._env._episode_iterator._shuffle_iterator()

    def save_trajectory_data(self):
        if "replica" in self.scene_name:
            folder = self.args.save_trajectory_data + "/" + \
                        self.scene_name.split("/")[-3]+"/"
        else:
            folder = self.args.save_trajectory_data + "/" + \
                        self.scene_name.split("/")[-1].split(".")[0]+"/"
        if not os.path.exists(folder):
            os.makedirs(folder)
        filepath = folder+str(self.episode_no)+".txt"
        with open(filepath, "w+") as f:
            f.write(self.scene_name+"\n")
            for state in self.trajectory_states:
                f.write(str(state)+"\n")
            f.flush()

    def save_position(self):
        self.agent_state = self._env.sim.get_agent_state()
        self.trajectory_states.append([self.agent_state.position,
                                       self.agent_state.rotation])


    def reset(self):
        args = self.args
        self.episode_no += 1
        self.timestep = 0
        self._previous_action = None
        self.trajectory_states = []

        if args.randomize_env_every > 0:
            if np.mod(self.episode_no, args.randomize_env_every) == 0:
                self.randomize_env()

        # Get Ground Truth Map
        self.explorable_map = None
        self.gt_area = [0.,0.] #ground-truth areas of each map: bounding box and explorable area  
        while self.explorable_map is None:
            obs = super().reset()
            full_map_size = args.map_size_cm//args.map_resolution
            self.explorable_map = self._get_gt_map(full_map_size)
        self.prev_explored_area = 0.

        # Preprocess observations
        rgb = obs['rgb'].astype(np.uint8)
        self.obs = rgb # For visualization
        if self.args.frame_width != self.args.env_frame_width:
            rgb = np.asarray(self.res(rgb))
        state = rgb.transpose(2, 0, 1)
        depth = _preprocess_depth(obs['depth'])

        # Initialize map and pose
        self.map_size_cm = args.map_size_cm
        self.mapper.reset_map(self.map_size_cm)
        self.loc_0 = [int(full_map_size/2), int(full_map_size/2), 0]
        self.explore_prop = 0.
        self.curr_loc = [self.map_size_cm/100.0/2.0,
                         self.map_size_cm/100.0/2.0, 0.]
        self.curr_loc_gt = self.curr_loc
        self.last_loc_gt = self.curr_loc_gt
        self.last_loc = self.curr_loc
        self.last_sim_location = self.get_sim_location()

        # Convert pose to cm and degrees for mapper
        mapper_gt_pose = (self.curr_loc_gt[0]*100.0,
                          self.curr_loc_gt[1]*100.0,
                          np.deg2rad(self.curr_loc_gt[2]))

        # Update ground_truth map and explored area
        fp_proj, self.map, fp_explored, self.explored_map, self.new_explored, self.frontier, self.frontier_clusters = \
            self.mapper.update_map(depth, mapper_gt_pose)

        # Initialize variables
        self.scene_name = self.habitat_env.sim.habitat_config.SCENE
        self.visited = np.zeros(self.map.shape)
        self.visited_vis = np.zeros(self.map.shape)
        self.visited_gt = np.zeros(self.map.shape)
        self.collison_map = np.zeros(self.map.shape)
        self.visited_count = np.zeros(self.map.shape)
        self.fmm_dist = np.zeros(self.map.shape)
        self.num_explored = []
        self.num_forward = []
        self.num_forward_step = 0
        self.col_width = 1
        self.collision_flag = 0
        self.goal = [0,0]
        self.goal_arbitrary = [0,0]
        self.option = 0
        self.change_goal_flag = False

        # Set info
        self.info = {
            'time': self.timestep,
            'fp_proj': self.map,
            'fp_explored': self.explored_map,
            'sensor_pose': [0., 0., 0.],
            'pose_err': [0., 0., 0.],
        }

        self.save_position()

        return state, self.info

    def step(self, action):

        args = self.args
        self.timestep += 1

        # Action remapping
        if action == 2: # Forward
            self.num_forward_step += 1
            action = 1
            noisy_action = HabitatSimActions.NOISY_FORWARD
        elif action == 1: # Right
            action = 3
            noisy_action = HabitatSimActions.NOISY_RIGHT
        elif action == 0: # Left
            action = 2
            noisy_action = HabitatSimActions.NOISY_LEFT

        self.last_loc = np.copy(self.curr_loc)
        self.last_loc_gt = np.copy(self.curr_loc_gt)
        self._previous_action = action

        if args.noisy_actions:
            obs, rew, done, info = super().step(noisy_action) #original: noisy_action
        else:
            obs, rew, done, info = super().step(action)

        # Preprocess observations
        rgb = obs['rgb'].astype(np.uint8)
        self.obs = rgb # For visualization
        if self.args.frame_width != self.args.env_frame_width:
            rgb = np.asarray(self.res(rgb))

        state = rgb.transpose(2, 0, 1)

        depth = _preprocess_depth(obs['depth'])

        # Get base sensor and ground-truth pose
        dx_gt, dy_gt, do_gt = self.get_gt_pose_change()
        dx_base, dy_base, do_base = self.get_base_pose_change(
                                        action, (dx_gt, dy_gt, do_gt))

        self.curr_loc = pu.get_new_pose(self.curr_loc,
                               (dx_base, dy_base, do_base))

        self.curr_loc_gt = pu.get_new_pose(self.curr_loc_gt,
                               (dx_gt, dy_gt, do_gt))

        if not args.noisy_odometry: # default = False (noisy odometry is on, but noisy actions is off).
            self.curr_loc = self.curr_loc_gt
            dx_base, dy_base, do_base = dx_gt, dy_gt, do_gt

        # Convert pose to cm and degrees for mapper
        mapper_gt_pose = (self.curr_loc_gt[0]*100.0,
                          self.curr_loc_gt[1]*100.0,
                          np.deg2rad(self.curr_loc_gt[2]))


        # Update ground_truth map and explored area
        fp_proj, self.map, fp_explored, self.explored_map, self.new_explored, self.frontier, self.frontier_clusters = \
                self.mapper.update_map(depth, mapper_gt_pose)

        self.collision_flag = 0
        # Update collision map
        if action == 1:
            x1, y1, t1 = self.last_loc
            x2, y2, t2 = self.curr_loc
            if abs(x1 - x2)< 0.05 and abs(y1 - y2) < 0.05:
                self.col_width += 2
                self.col_width = min(self.col_width, 10)
            else:
                self.col_width = 1

            dist = pu.get_l2_distance(x1, x2, y1, y2)
            if dist < args.collision_threshold: #Collision
                self.collision_flag = 1
                length = 2
                width = self.col_width
                buf = 3
                for i in range(length):
                    for j in range(width):
                        wx = x1 + 0.05*((i+buf) * np.cos(np.deg2rad(t1)) + \
                                        (j-width//2) * np.sin(np.deg2rad(t1)))
                        wy = y1 + 0.05*((i+buf) * np.sin(np.deg2rad(t1)) - \
                                        (j-width//2) * np.cos(np.deg2rad(t1)))
                        r, c = wy, wx
                        r, c = int(r*100/args.map_resolution), \
                               int(c*100/args.map_resolution)
                        [r, c] = pu.threshold_poses([r, c],
                                    self.collison_map.shape)
                        self.collison_map[r,c] = 1

        self.visited_count[self.new_explored==1] += 1 

        # Set info
        self.info['time'] = self.timestep
        self.info['fp_proj'] = self.map, # occupancy map
        self.info['fp_explored']= self.explored_map, # explored map
        self.info['sensor_pose'] = [self.curr_loc_gt[0],
                                    self.curr_loc_gt[1],
                                    np.deg2rad(self.curr_loc_gt[2])]


        if self.timestep % args.num_local_steps == 0:
            area, ratio = self.get_crafted_reward()
            self.info['exp_reward'] = area 
            self.info['exp_ratio'] = ratio
        else:
            self.info['exp_reward'] = None
            self.info['exp_ratio'] = None


        self.save_position()

        if self.info['time'] >= args.max_episode_length:
            done = True

            if self.args.eval == 1: #testing phase only
                mat_dir = f"{self.args.exp_output}{self.args.exp_name}/" # is "exp_data/exp#/"
                if not os.path.exists(mat_dir):
                    print(mat_dir)                    
                    os.makedirs(mat_dir)
                savemat(f"{mat_dir}{self.rank}-{self.episode_no}.mat", {"num_explored": self.num_explored,"num_forward": self.num_forward,"gt_area": self.gt_area})
                

            if self.args.save_trajectory_data != "0":
                self.save_trajectory_data()
        else:
            done = False

        return state, rew, done, self.info

    def get_reward_range(self):
        # This function is not used, Habitat-RLEnv requires this function
        return (0., 1.0)

    def get_reward(self, observations):
        # This function is not used, Habitat-RLEnv requires this function
        return 0.

    def get_crafted_reward(self):  # this reward function is used
        curr_explored = self.explored_map * self.explorable_map
        explorable = self.explorable_map
        if self.local_explore_width:  # if local explore width given, scale to local region.
            curr_explored = curr_explored[self.loc_0[1] - self.local_explore_width:self.loc_0[1] + self.local_explore_width + 1,
                        self.loc_0[0] - self.local_explore_width:self.loc_0[0] + self.local_explore_width + 1]
            explorable = explorable[self.loc_0[1] - self.local_explore_width:self.loc_0[1] + self.local_explore_width + 1,
                          self.loc_0[0] - self.local_explore_width:self.loc_0[0] + self.local_explore_width + 1]
        curr_explored_area = curr_explored.sum()
        explorable_area = explorable.sum()

        curr_explore_ratio = curr_explored_area / explorable_area
        self.explore_prop = curr_explore_ratio

        step_explored_area = (curr_explored_area - self.prev_explored_area)*1.
        step_explore_reward = step_explored_area / explorable_area
        step_explore_reward = step_explore_reward * 25./10000.  # converting to m^2
        step_explore_reward *= 0.02  # Reward Scaling
        self.prev_explored_area = curr_explored_area

        return step_explore_reward, curr_explore_ratio

    def get_global_reward_old2(self):
        self.new_explored = self.new_explored*self.explorable_map
        ind = np.flatnonzero(self.new_explored)

        if ind.size == 0:
            return -0.1,-0.1

        m_reward = (1./(np.sqrt(self.visited_count.ravel()[ind]*self.new_explored.ravel()[ind]))).sum()

        reward_scale = self.new_explored.sum()
        m_ratio = m_reward/reward_scale

        #m_reward *= 0.02 # Reward Scaling

        return m_ratio, m_ratio

    def get_global_reward(self):

        #novelty rewards
        self.new_explored = self.new_explored*self.explorable_map
        ind = np.flatnonzero(self.new_explored)

        if ind.size == 0:
            m_ratio = 0.
        else:
            m_reward = (1./(np.sqrt(self.visited_count.ravel()[ind]*self.new_explored.ravel()[ind]))).sum()
            reward_scale = self.new_explored.sum()
            m_ratio = m_reward/reward_scale

        #m_reward *= 0.02 # Reward Scaling

        #frontier rewards
        r, c = [int(self.curr_loc_gt[1] * 100.0 / self.args.map_resolution),
                int(self.curr_loc_gt[0] * 100.0 / self.args.map_resolution)]
        m_reward = bfs(self.map, self.explored_map, self.frontier, (r,c))
        if not m_reward == 0:
            m_reward = 1./m_reward

        print(m_reward)
        return m_ratio+m_reward, m_ratio+m_reward


    def get_done(self, observations):
        # This function is not used, Habitat-RLEnv requires this function
        return False

    def get_info(self, observations):
        # This function is not used, Habitat-RLEnv requires this function
        info = {}
        return info

    def seed(self, seed):
        self.rng = np.random.RandomState(seed)

    def get_spaces(self):
        return self.observation_space, self.action_space

    def build_mapper(self):
        params = {}
        params['frame_width'] = self.args.env_frame_width
        params['frame_height'] = self.args.env_frame_height
        params['fov'] =  self.args.hfov
        params['resolution'] = self.args.map_resolution #5
        params['map_size_cm'] = self.args.map_size_cm
        params['agent_min_z'] = 25
        params['agent_max_z'] = 150
        params['agent_height'] = self.args.camera_height * 100
        params['agent_view_angle'] = 0
        params['du_scale'] = self.args.du_scale
        params['vision_range'] = self.args.vision_range
        params['visualize'] = self.args.visualize
        params['obs_threshold'] = self.args.obs_threshold #1
        params['num_maps'] = self.args.num_maps
        self.selem = skimage.morphology.disk(self.args.obstacle_boundary /
                                             self.args.map_resolution)
        mapper = MapBuilder(params)
        return mapper


    def get_sim_location(self):
        agent_state = super().habitat_env.sim.get_agent_state(0)
        x = -agent_state.position[2]
        y = -agent_state.position[0]
        axis = quaternion.as_euler_angles(agent_state.rotation)[0]
        if (axis%(2*np.pi)) < 0.1 or (axis%(2*np.pi)) > 2*np.pi - 0.1:
            o = quaternion.as_euler_angles(agent_state.rotation)[1]
        else:
            o = 2*np.pi - quaternion.as_euler_angles(agent_state.rotation)[1]
        if o > np.pi:
            o -= 2 * np.pi
        return x, y, o


    def get_gt_pose_change(self):
        curr_sim_pose = self.get_sim_location()
        dx, dy, do = pu.get_rel_pose_change(curr_sim_pose, self.last_sim_location)
        self.last_sim_location = curr_sim_pose
        return dx, dy, do


    def get_base_pose_change(self, action, gt_pose_change):
        dx_gt, dy_gt, do_gt = gt_pose_change
        if action == 1: ## Forward
            x_err, y_err, o_err = self.sensor_noise_fwd.sample()[0][0]
        elif action == 3: ## Right
            x_err, y_err, o_err = self.sensor_noise_right.sample()[0][0]
        elif action == 2: ## Left
            x_err, y_err, o_err = self.sensor_noise_left.sample()[0][0]
        else: ##Stop
            x_err, y_err, o_err = 0., 0., 0.

        x_err = x_err * self.args.noise_level # noise_level=1.0
        y_err = y_err * self.args.noise_level
        o_err = o_err * self.args.noise_level
        return dx_gt + x_err, dy_gt + y_err, do_gt + np.deg2rad(o_err)


    def get_short_term_goal(self, inputs):

        args = self.args

        if inputs['active'] == False:
            output = np.zeros((args.goals_size + 2)) #2+2=4
            return output

        # Get Map prediction
        map_pred = inputs['map_pred']
        exp_pred = inputs['exp_pred']

        grid = np.rint(map_pred) #round to nearest integer (-1.5 -> -2 and 1.5 -> 2)
        explored = np.rint(exp_pred)

        # Get pose prediction and global policy planning window
        start_x, start_y, start_o, gx1, gx2, gy1, gy2 = inputs['pose_pred']
        gx1, gx2, gy1, gy2 = int(gx1), int(gx2), int(gy1), int(gy2)
        planning_window = [gx1, gx2, gy1, gy2]

        # Get last loc
        last_start_x, last_start_y = self.last_loc[0], self.last_loc[1] # in m.
        r, c = last_start_y, last_start_x
        last_start = [int(r * 100.0/args.map_resolution - gx1), #convert to map coords.
                      int(c * 100.0/args.map_resolution - gy1)]
        last_start = pu.threshold_poses(last_start, grid.shape) #clipping to within the grid shape.

        # Get curr loc
        self.curr_loc = [start_x, start_y, start_o]
        r, c = start_y, start_x # in m.
        start = [int(r * 100.0/args.map_resolution - gx1), #convert to map coords.
                 int(c * 100.0/args.map_resolution - gy1)]
        start = pu.threshold_poses(start, grid.shape) #clipping to within the grid shape.
        #TODO: try reducing this

        self.visited[gx1:gx2, gy1:gy2][start[0]-2:start[0]+3,
                                       start[1]-2:start[1]+3] = 1

        steps = 25
        for i in range(steps):
            x = int(last_start[0] + (start[0] - last_start[0]) * (i+1) / steps)
            y = int(last_start[1] + (start[1] - last_start[1]) * (i+1) / steps)
            self.visited_vis[gx1:gx2, gy1:gy2][x, y] = 1

        # Get last loc ground truth pose
        last_start_x, last_start_y = self.last_loc_gt[0], self.last_loc_gt[1]
        r, c = last_start_y, last_start_x
        last_start = [int(r * 100.0/args.map_resolution),
                      int(c * 100.0/args.map_resolution)]
        last_start = pu.threshold_poses(last_start, self.visited_gt.shape)

        # Get ground truth pose
        start_x_gt, start_y_gt, start_o_gt = self.curr_loc_gt
        r, c = start_y_gt, start_x_gt
        start_gt = [int(r * 100.0/args.map_resolution),
                    int(c * 100.0/args.map_resolution)]
        start_gt = pu.threshold_poses(start_gt, self.visited_gt.shape)
        #self.visited_gt[start_gt[0], start_gt[1]] = 1

        steps = 25 #???
        for i in range(steps):
            x = int(last_start[0] + (start_gt[0] - last_start[0]) * (i+1) / steps)
            y = int(last_start[1] + (start_gt[1] - last_start[1]) * (i+1) / steps)
            self.visited_gt[x, y] = 1


        # Get goal
        
        goal = inputs['goal'] # if num_maps=6, this is a frontier point goal.
        goal_arbitrary = inputs['goal_arbitrary'] # is the arbitrary point from the policy.
        
        self.change_goal_flag = inputs['change_goal'] # True, if navigation. False, if look-around.
        #goal[0] = 500
        #goal[1] = 20
        goal = pu.threshold_poses(goal, grid.shape)
        self.goal = goal
        self.goal_arbitrary = goal_arbitrary # brought in purely for visualization.

        # Get short-term goal
        #stg = self._get_stg(grid, explored, start, np.copy(goal), planning_window)

        # Find GT action

        #print("hello!")
        #print(self.explorable_map[int(goal[1]), int(goal[0])])
        if self.args.eval == 1: # same either way.

            gt_action, stg_x_gt, stg_y_gt, self.fmm_dist, goal_reached, new_goal = self._get_gt_action((1 - self.explorable_map), start,  #self.map
                                            [int(goal[0]), int(goal[1])],
                                            planning_window, start_o_gt)
        else:
            gt_action, stg_x_gt, stg_y_gt, self.fmm_dist, goal_reached, new_goal = self._get_gt_action((1 - self.explorable_map), start, #self.map
                                            [int(goal[0]), int(goal[1])],
                                            planning_window, start_o_gt) #new_goal out = goal in.
            

        output = np.zeros((args.goals_size + 2)) #resetting before filling

        

        output[0] = goal_reached#int((relative_angle%360.)/5.)
        output[1] = int(new_goal[0])#discretize(relative_dist)
        output[2] = int(new_goal[1])
        output[3] = gt_action # (0:right, 1:left, 2:forward)
        return output


    def update_visualize(self, option):

        args = self.args
        self.option = option

        if option == 0:
            self.change_goal_flag = False

        # Get last loc ground truth pose
        last_start_x, last_start_y = self.last_loc_gt[0], self.last_loc_gt[1]
        r, c = last_start_y, last_start_x
        last_start = [int(r * 100.0/args.map_resolution),
                      int(c * 100.0/args.map_resolution)]
        last_start = pu.threshold_poses(last_start, self.visited_gt.shape)

        # Get ground truth pose
        start_x_gt, start_y_gt, start_o_gt = self.curr_loc_gt
        r, c = start_y_gt, start_x_gt
        start_gt = [int(r * 100.0/args.map_resolution),
                    int(c * 100.0/args.map_resolution)]
        start_gt = pu.threshold_poses(start_gt, self.visited_gt.shape)
        #self.visited_gt[start_gt[0], start_gt[1]] = 1

        steps = args.num_local_steps
        for i in range(steps):
            x = int(last_start[0] + (start_gt[0] - last_start[0]) * (i+1) / steps)
            y = int(last_start[1] + (start_gt[1] - last_start[1]) * (i+1) / steps)
            self.visited_gt[x, y] = 1


        if args.visualize or args.print_images:
            ep_dir = '{}/episodes/{}/{}/'.format(
                            self.dump_dir, self.rank+1, self.episode_no)
            if not os.path.exists(ep_dir):
                os.makedirs(ep_dir)

            # if local explore width given, is scaled to local region.
            self.num_explored.append(self.explore_prop)
            self.num_forward.append(self.num_forward_step)

            #if self.rank != 0:
            #    return
            vis_grid = vu.get_colored_map(self.map,
                            self.collison_map,
                            self.visited_gt,
                            (self.goal[0], self.goal[1]),
                            (self.goal_arbitrary[0], self.goal_arbitrary[1]),
                            self.explored_map,
                            self.explorable_map,
                            self.frontier, 
                            self.frontier_clusters,
                            self.local_explore_width,
                            self.change_goal_flag)
            vis_grid = np.flipud(vis_grid)

            '''
            if self.timestep % 100 == 99:
                args.print_images = 1
            else:
                args.print_images = 0
            '''

            vu.visualize(option, self.figure, self.ax, self.obs, vis_grid[:,:,::-1], self.fmm_dist, self.num_explored, #(1 - self.explorable_map)*self.map 
                        (start_x_gt, start_y_gt, start_o_gt),
                        (self.goal[0], self.goal[1]),
                        self.dump_dir, self.rank, self.episode_no,
                        self.timestep, args.visualize,
                        args.print_images, args.vis_type, args.max_episode_length)
           

    def _get_gt_map(self, full_map_size):
        self.scene_name = self.habitat_env.sim.habitat_config.SCENE
        logger.error('Computing map for %s', self.scene_name)

        # Get map in habitat simulator coordinates
        self.map_obj = HabitatMaps(self.habitat_env)
        if self.map_obj.size[0] < 1 or self.map_obj.size[1] < 1:
            logger.error("Invalid map: {}/{}".format(
                            self.scene_name, self.episode_no))
            return None

        agent_y = self._env.sim.get_agent_state().position.tolist()[1]*100.
        sim_map = self.map_obj.get_map(agent_y, -50., 50.0)

        sim_map[sim_map > 0] = 1.

        self.gt_area[0] = sim_map.shape[0] * sim_map.shape[1]
        self.gt_area[1] = sim_map.sum()

        # Transform the map to align with the agent
        min_x, min_y = self.map_obj.origin/100.0
        x, y, o = self.get_sim_location()
        x, y = -x - min_x, -y - min_y
        range_x, range_y = self.map_obj.max/100. - self.map_obj.origin/100.

        map_size = sim_map.shape
        scale = 2.
        grid_size = int(scale*max(map_size))
        grid_map = np.zeros((grid_size, grid_size))

        grid_map[(grid_size - map_size[0])//2:
                 (grid_size - map_size[0])//2 + map_size[0],
                 (grid_size - map_size[1])//2:
                 (grid_size - map_size[1])//2 + map_size[1]] = sim_map

        if map_size[0] > map_size[1]:
            st = torch.tensor([[
                    (x - range_x/2.) * 2. / (range_x * scale) \
                             * map_size[1] * 1. / map_size[0],
                    (y - range_y/2.) * 2. / (range_y * scale),
                    180.0 + np.rad2deg(o)
                ]])

        else:
            st = torch.tensor([[
                    (x - range_x/2.) * 2. / (range_x * scale),
                    (y - range_y/2.) * 2. / (range_y * scale) \
                            * map_size[0] * 1. / map_size[1],
                    180.0 + np.rad2deg(o)
                ]])

        rot_mat, trans_mat = get_grid(st, (1, 1,
            grid_size, grid_size), torch.device("cpu"))

        grid_map = torch.from_numpy(grid_map).float()
        grid_map = grid_map.unsqueeze(0).unsqueeze(0)
        translated = F.grid_sample(grid_map, trans_mat)
        rotated = F.grid_sample(translated, rot_mat)

        episode_map = torch.zeros((full_map_size, full_map_size)).float()
        if full_map_size > grid_size:
            episode_map[(full_map_size - grid_size)//2:
                        (full_map_size - grid_size)//2 + grid_size,
                        (full_map_size - grid_size)//2:
                        (full_map_size - grid_size)//2 + grid_size] = \
                                rotated[0,0]
        else:
            episode_map = rotated[0,0,
                              (grid_size - full_map_size)//2:
                              (grid_size - full_map_size)//2 + full_map_size,
                              (grid_size - full_map_size)//2:
                              (grid_size - full_map_size)//2 + full_map_size]



        episode_map = episode_map.numpy()
        episode_map[episode_map > 0] = 1.

        return episode_map


    def _get_stg(self, grid, explored, start, goal, planning_window): #don't think this is used

        [gx1, gx2, gy1, gy2] = planning_window

        x1 = min(start[0], goal[0])
        x2 = max(start[0], goal[0])
        y1 = min(start[1], goal[1])
        y2 = max(start[1], goal[1])
        dist = pu.get_l2_distance(goal[0], start[0], goal[1], start[1])
        buf = max(20., dist)
        x1 = max(1, int(x1 - buf))
        x2 = min(grid.shape[0]-1, int(x2 + buf))
        y1 = max(1, int(y1 - buf))
        y2 = min(grid.shape[1]-1, int(y2 + buf))

        rows = explored.sum(1)
        rows[rows>0] = 1
        ex1 = np.argmax(rows)
        ex2 = len(rows) - np.argmax(np.flip(rows))

        cols = explored.sum(0)
        cols[cols>0] = 1
        ey1 = np.argmax(cols)
        ey2 = len(cols) - np.argmax(np.flip(cols))

        ex1 = min(int(start[0]) - 2, ex1)
        ex2 = max(int(start[0]) + 2, ex2)
        ey1 = min(int(start[1]) - 2, ey1)
        ey2 = max(int(start[1]) + 2, ey2)

        x1 = max(x1, ex1)
        x2 = min(x2, ex2)
        y1 = max(y1, ey1)
        y2 = min(y2, ey2)

        traversible = skimage.morphology.binary_dilation(
                        grid[x1:x2, y1:y2],
                        self.selem) != True
        traversible[self.collison_map[gx1:gx2, gy1:gy2][x1:x2, y1:y2] == 1] = 0
        traversible[self.visited[gx1:gx2, gy1:gy2][x1:x2, y1:y2] == 1] = 1

        traversible[int(start[0]-x1)-1:int(start[0]-x1)+2,
                    int(start[1]-y1)-1:int(start[1]-y1)+2] = 1

        if goal[0]-2 > x1 and goal[0]+3 < x2\
            and goal[1]-2 > y1 and goal[1]+3 < y2:
            traversible[int(goal[0]-x1)-2:int(goal[0]-x1)+3,
                    int(goal[1]-y1)-2:int(goal[1]-y1)+3] = 1
        else:
            goal[0] = min(max(x1, goal[0]), x2)
            goal[1] = min(max(y1, goal[1]), y2)

        def add_boundary(mat):
            h, w = mat.shape
            new_mat = np.ones((h+2,w+2))
            new_mat[1:h+1,1:w+1] = mat
            return new_mat

        traversible = add_boundary(traversible)

        planner = FMMPlanner(traversible, 360//self.dt)

        reachable = planner.set_goal([goal[1]-y1+1, goal[0]-x1+1])

        stg_x, stg_y = start[0] - x1 + 1, start[1] - y1 + 1
        for i in range(self.args.short_goal_dist):
            stg_x, stg_y, replan = planner.get_short_term_goal([stg_x, stg_y])
        if replan:
            stg_x, stg_y = start[0], start[1]
        else:
            stg_x, stg_y = stg_x + x1 - 1, stg_y + y1 - 1

        return (stg_x, stg_y)

    def _get_gt_action(self, grid, start, goal, planning_window, start_o):
        
        [gx1, gx2, gy1, gy2] = planning_window

        x1 = min(start[0], goal[0])
        x2 = max(start[0], goal[0])
        y1 = min(start[1], goal[1])
        y2 = max(start[1], goal[1])
        dist = pu.get_l2_distance(goal[0], start[0], goal[1], start[1])

        if dist < 4:
            goal_reached = True
        else:
            goal_reached = False

        buf = max(20., dist)
        x1 = max(0, int(x1 - buf))
        x2 = min(grid.shape[0], int(x2 + buf))
        y1 = max(0, int(y1 - buf))
        y2 = min(grid.shape[1], int(y2 + buf))

        x1 = 0
        y1 = 0
        x2 = grid.shape[0]
        y2 = grid.shape[1]



        path_found = False
        goal_r = 0
        while not path_found:
        
            traversible = skimage.morphology.binary_dilation(
            grid[gx1:gx2, gy1:gy2][x1:x2, y1:y2],
            self.selem) != True

            traversible[self.visited[gx1:gx2, gy1:gy2][x1:x2, y1:y2] == 1] = 1
            traversible[int(start[0]-x1)-1:int(start[0]-x1)+2,
                        int(start[1]-y1)-1:int(start[1]-y1)+2] = 1
            traversible[int(goal[0]-x1)-goal_r:int(goal[0]-x1)+goal_r+1,
                        int(goal[1]-y1)-goal_r:int(goal[1]-y1)+goal_r+1] = 1
            scale = 1

            planner = FMMPlanner(traversible, 360//self.dt, scale)

            reachable = planner.set_goal([goal[1]-y1, goal[0]-x1])

            stg_x_gt, stg_y_gt = start[0] - x1, start[1] - y1
            for i in range(1):
                stg_x_gt, stg_y_gt, replan, fmm_dist = \
                        planner.get_short_term_goal([stg_x_gt, stg_y_gt])

            if replan and buf < 100.:
                buf = 2*buf
                x1 = max(0, int(x1 - buf))
                x2 = min(grid.shape[0], int(x2 + buf))
                y1 = max(0, int(y1 - buf))
                y2 = min(grid.shape[1], int(y2 + buf))
            elif replan and goal_r < 50:
                goal_r += 1
            else:
                path_found = True

        stg_x_gt, stg_y_gt = stg_x_gt + x1, stg_y_gt + y1



        #print("start")
        #print((start[0],start[1]))

        #print("next")
        #print((stg_x_gt,stg_y_gt))

        angle_st_goal = math.degrees(math.atan2(stg_x_gt - start[0],
                                                stg_y_gt - start[1]))

        #print("angle_st_goal")
        #print(angle_st_goal)

        angle_agent = (start_o)%360.0
        if angle_agent > 180:
            angle_agent -= 360

        #print("angle_agent")
        #print(angle_agent)

        relative_angle = (angle_agent - angle_st_goal)%360.0
        if relative_angle > 180:
            relative_angle -= 360

        if relative_angle > 15.: #if planned action creates relative angle (current angle - stg_angle) > 15:
            gt_action = 1 #turn right one step (to correct it)
        elif relative_angle < -15.: #if planned action creates relative angle (current angle - stg_angle) < -15:
            gt_action = 0 #turn left one step (to correct)
        else: # if proposed stg_angle is within the -15 to +15 bounds: then move forward towards the st_goal.
            gt_action = 2 #forward.

        #print("relative angle")
        #print(relative_angle)

        return gt_action, stg_x_gt, stg_y_gt, fmm_dist, goal_reached, goal
        
        
        
    def _get_gt_action_not_used(self, grid, start, goal, planning_window, start_o):
        
        #timer.tic()
        #print("Timer for phase 1")
        #timer.toc()
        #timer.tic()

        [gx1, gx2, gy1, gy2] = planning_window

        x1 = min(start[0], goal[0])
        x2 = max(start[0], goal[0])
        y1 = min(start[1], goal[1])
        y2 = max(start[1], goal[1])
        dist = pu.get_l2_distance(goal[0], start[0], goal[1], start[1])

        buf = max(20., dist)
        x1 = max(0, int(x1 - buf))
        x2 = min(grid.shape[0], int(x2 + buf))
        y1 = max(0, int(y1 - buf))
        y2 = min(grid.shape[1], int(y2 + buf))

        x1 = 0
        y1 = 0
        x2 = grid.shape[0]
        y2 = grid.shape[1]


        path_found = False

        
        goal_r = 0

        traversible = skimage.morphology.binary_dilation(
                        grid[gx1:gx2, gy1:gy2][x1:x2, y1:y2],
                        self.selem) != True
        
        traversible[self.collison_map[gx1:gx2, gy1:gy2][x1:x2, y1:y2] == 1] = 0
        traversible[self.visited[gx1:gx2, gy1:gy2][x1:x2, y1:y2] == 1] = 1

        goal_reachable = traversible[int(goal[0]-x1)-goal_r:int(goal[0]-x1)+goal_r+1,
                        int(goal[1]-y1)-goal_r:int(goal[1]-y1)+goal_r+1]

        #print("Timer for phase 2")
        #timer.toc()
        #timer.tic()
        
        '''
        while not goal_reachable:
             goal_r += 1
             for i in range(max(0, int(goal[0]-x1)-goal_r),min(grid.shape[0], int(goal[0]-x1)+goal_r+1)):
                 for j in range(max(0, int(goal[1]-x1)-goal_r),min(grid.shape[1], int(goal[1]-x1)+goal_r+1)):
                     if traversible[i,j] == 1:
                         goal_reachable = True
                         goal[0] = i
                         goal[1] = j
                         break
                 if goal_reachable:
                     break
                     
        '''
        
        #print("goal r" + str(self.rank))
        #print(goal_r)
        
        #print("Timer for phase 3")
        #timer.toc()
        #timer.tic()
        
        traversible = skimage.morphology.binary_dilation(
                grid[gx1:gx2, gy1:gy2][x1:x2, y1:y2],
                self.selem) != True
        #traversible = skimage.morphology.binary_closing(traversible, self.selem)

        goal_r = 0
        while not path_found and goal_r < 50:
                            
            traversible[self.collison_map[gx1:gx2, gy1:gy2][x1:x2, y1:y2] == 1] = 0
            traversible[self.visited[gx1:gx2, gy1:gy2][x1:x2, y1:y2] == 1] = 1

            traversible[int(start[0]-x1)-1:int(start[0]-x1)+2,
                        int(start[1]-y1)-1:int(start[1]-y1)+2] = 1

            traversible[int(goal[0]-x1)-goal_r:int(goal[0]-x1)+goal_r+1,
                        int(goal[1]-y1)-goal_r:int(goal[1]-y1)+goal_r+1] = 1

            scale = 1
            
            #print(traversible)

            planner = FMMPlanner(traversible, 360//self.dt, scale)

            reachable = planner.set_goal([goal[1]-y1, goal[0]-x1])

            stg_x_gt, stg_y_gt = start[0] - x1, start[1] - y1
            for i in range(1):
                stg_x_gt, stg_y_gt, replan, fmm_dist = \
                        planner.get_short_term_goal([stg_x_gt, stg_y_gt])

            if replan and buf < 100.:
                buf = 2*buf
                x1 = max(0, int(x1 - buf))
                x2 = min(grid.shape[0], int(x2 + buf))
                y1 = max(0, int(y1 - buf))
                y2 = min(grid.shape[1], int(y2 + buf))
            elif replan and goal_r < 50:
                goal_r += 1
            else:
                path_found = True

        stg_x_gt, stg_y_gt = stg_x_gt + x1, stg_y_gt + y1


        if path_found and dist <= max(1.4*goal_r,10):
            goal_reached = True
        else:
            goal_reached = False

        #print("goal_r " + str(goal_r))
        #print("Timer for phase 4")
        #timer.toc()
        #timer.tic()
        #print("start")
        #print((start[0],start[1]))

        #print("next")
        #print((stg_x_gt,stg_y_gt))

        angle_st_goal = math.degrees(math.atan2(stg_x_gt - start[0],
                                                stg_y_gt - start[1]))

        #print("angle_st_goal")
        #print(angle_st_goal)

        angle_agent = (start_o)%360.0
        if angle_agent > 180:
            angle_agent -= 360

        #print("angle_agent")
        #print(angle_agent)

        relative_angle = (angle_agent - angle_st_goal)%360.0
        if relative_angle > 180:
            relative_angle -= 360

        if relative_angle > 15.:
            gt_action = 1
        elif relative_angle < -15.:
            gt_action = 0
        else:
            gt_action = 2

        #print("relative angle")
        #print(relative_angle)
        #print("Timer for phase 5")
        #timer.toc()
        #timer.tic()
        return gt_action, stg_x_gt, stg_y_gt, fmm_dist, goal_reached | (path_found == False), goal
