#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
r"""
This file hosts task-specific or trainer-specific environments for trainers.
All environments here should be a (direct or indirect ) subclass of Env class
in habitat. Customized environments should be registered using
``@baseline_registry.register_env(name="myEnv")` for reusability
"""

from typing import Optional, Type
import math
import habitat
# from habitat import Config, Dataset
from habitat_baselines.common.baseline_registry import baseline_registry

# NavSLAMRLEnv
import os
from habitat import logger
from habitat_baselines.common.baseline_registry import baseline_registry
from habitat import Config, Env, RLEnv, Dataset

from collections import Counter
import quaternion
import skimage.morphology
import numpy as np
from torchvision import transforms
import torch
from torch.nn import functional as F
from PIL import Image
import matplotlib
import time
import matplotlib.pyplot as plt

from habitat_baselines.common.map_utils.supervision import HabitatMaps
import habitat_baselines.common.map_utils.pose as pu
import habitat_baselines.common.map_utils.visualizations as vu
from habitat_baselines.common.map_utils.model import get_grid
from habitat_baselines.common.map_utils.map_builder import MapBuilder
from habitat_baselines.common.map_utils.fmm_planner import FMMPlanner


def get_env_class(env_name: str) -> Type[habitat.RLEnv]:
    r"""Return environment class based on name.

    Args:
        env_name: name of the environment.

    Returns:
        Type[habitat.RLEnv]: env class.
    """
    print("env_name :", env_name)
    print("env_type :", baseline_registry.get_env(env_name))
    return baseline_registry.get_env(env_name)


@baseline_registry.register_env(name="NavRLEnv")
class NavRLEnv(habitat.RLEnv):
    def __init__(self, config: Config, dataset: Optional[Dataset] = None):
        self._rl_config = config.RL
        self._core_env_config = config.TASK_CONFIG
        self._reward_measure_name = self._rl_config.REWARD_MEASURE
        self._success_measure_name = self._rl_config.SUCCESS_MEASURE
        self.dataset = dataset

        self._previous_measure = None
        self._previous_action = None
        super().__init__(self._core_env_config, dataset)

    def reset(self):
        self._previous_action = None
        observations = super().reset()
        self._previous_measure = self._env.get_metrics()[
            self._reward_measure_name
        ]

        self.scene = self.habitat_env.sim.semantic_annotations()

        # print("************************************* current_episodes:", (self._env.current_episode.scene_id))
        # print("************************************* current_episodes:", (self._env.current_episode.episode_id))

        # for obj in self.scene.objects:
        #     if obj is not None:
        #         print(
        #             f"Object id:{obj.id}, category:{obj.category.name()}, index:{obj.category.index()}"
        #             f" center:{obj.aabb.center}, dims:{obj.aabb.sizes}"
        #         )

        if "semantic" in observations:
            observations["semantic"] = self._preprocess_semantic(observations["semantic"])
        # print("category_to_task_category_id: ", self.dataset.category_to_task_category_id)
        return observations

    def step(self, *args, **kwargs):
        self._previous_action = kwargs["action"]
        obs, rew, done, info = super().step(*args, **kwargs)

        if "semantic" in obs:
            obs["semantic"] = self._preprocess_semantic(obs["semantic"])
        return obs, rew, done, info

    def _preprocess_semantic(self, semantic):
        # print("*********semantic type: ", type(semantic))
        se = list(set(semantic.ravel()))
        # print(se) # []
        for i in range(len(se)):
            if self.scene.objects[se[i]] is not None and self.scene.objects[se[i]].category.name() in self.dataset.category_to_task_category_id:
                # print(self.scene.objects[se[i]].id) 
                # print(self.scene.objects[se[i]].category.index()) 
                # print(type(self.scene.objects[se[i]].category.index()) ) # int
                semantic[semantic==se[i]] = self.dataset.category_to_task_category_id[self.scene.objects[se[i]].category.name()]
                # print(self.scene.objects[se[i]+1].category.name())
            else :
                semantic[
                    semantic==se[i]
                    ] = 0
        semantic = semantic.astype(np.uint8)
        se = list(set(semantic.ravel()))
        # print("semantic: ", se) # []

        return semantic

    def get_reward_range(self):
        return (
            self._rl_config.SLACK_REWARD - 1.0,
            self._rl_config.SUCCESS_REWARD + 1.0,
        )

    def get_reward(self, observations):
        reward = self._rl_config.SLACK_REWARD

        current_measure = self._env.get_metrics()[self._reward_measure_name]

        reward += self._previous_measure - current_measure
        self._previous_measure = current_measure

        if self._episode_success():
            reward += self._rl_config.SUCCESS_REWARD

        return reward

    def _episode_success(self):
        return self._env.get_metrics()[self._success_measure_name]

    def get_done(self, observations):
        done = False
        if self._env.episode_over or self._episode_success():
            done = True
        return done

    def get_info(self, observations):
        return self.habitat_env.get_metrics()



@baseline_registry.register_env(name="NavSLAMRLEnv")
class NavSLAMRLEnv(NavRLEnv):
    def __init__(self, config: Config, dataset: Optional[Dataset] = None):
        self.dataset = dataset

        self.rank = config.NUM_PROCESSES

        self.print_images = 1

        self.figure, self.ax = plt.subplots(1,2, figsize=(6*16/9, 6),
                                                facecolor="whitesmoke",
                                                num="Thread {}".format(self.rank))
                                                
        self.episode_no = 0

        self.env_frame_width = config.TASK_CONFIG.SIMULATOR.DEPTH_SENSOR.WIDTH
        self.env_frame_height = config.TASK_CONFIG.SIMULATOR.DEPTH_SENSOR.HEIGHT
        self.hfov = config.TASK_CONFIG.SIMULATOR.DEPTH_SENSOR.HFOV
        self.map_resolution = config.RL.SLAMDDPPO.map_resolution
        self.map_size_cm = config.RL.SLAMDDPPO.map_size_cm
        self.agent_min_z = config.RL.SLAMDDPPO.agent_min_z
        self.agent_max_z = config.RL.SLAMDDPPO.agent_max_z
        self.camera_height = config.TASK_CONFIG.SIMULATOR.DEPTH_SENSOR.POSITION[1]
        self.agent_view_angle = config.RL.SLAMDDPPO.agent_view_angle
        self.du_scale = config.RL.SLAMDDPPO.du_scale
        self.vision_range = config.RL.SLAMDDPPO.vision_range
        self.visualize = config.RL.SLAMDDPPO.visualize
        self.obs_threshold = config.RL.SLAMDDPPO.obs_threshold
        self.obstacle_boundary = config.RL.SLAMDDPPO.obstacle_boundary

        self.collision_threshold = config.RL.SLAMDDPPO.collision_threshold
        self.vis_type = config.RL.SLAMDDPPO.vis_type # '1: Show predicted map, 2: Show GT map'
        self.global_downscaling = config.RL.SLAMDDPPO.global_downscaling

        # 创建地图
        self.object_len = len(dataset.category_to_task_category_id)

        self.mapper = self.build_mapper()
        self.local_map_size = self.map_size_cm//self.map_resolution
        self.full_map_size = self.local_map_size * self.global_downscaling

        self.full_map = np.zeros((self.full_map_size,
                             self.full_map_size,
                             3), dtype=np.float32)

        self.full_semantic_map = np.zeros((self.full_map.shape[0], 
                                    self.full_map.shape[1],     
                                    self.object_len), dtype=np.float32)

        super().__init__(config, dataset)

    
    def save_position(self):
        self.agent_state = self._env.sim.get_agent_state()
        self.trajectory_states.append([self.agent_state.position,
                                       self.agent_state.rotation])


    def reset(self):
        self.episode_no += 1
        self.timestep = 0
        self._previous_action = None
        self.trajectory_states = []
        # print("reset")
        # start_time = time.clock()
        # Get Ground Truth Map
        self.explorable_map = None
        obs = super().reset()

        rgb = obs['rgb'].astype(np.uint8)
        self.obs = rgb # For visualization
        depth = _preprocess_depth(obs['depth'])
        semantic = obs['semantic']
        self.semantic = semantic
        self.object_ind = obs["objectgoal"]
        self.object_name = list(self.dataset.category_to_task_category_id.keys())[list(self.dataset.category_to_task_category_id.values()).index(self.object_ind)]
        
        # print(self.object_name)

        # Initialize global map and pose
        self.full_map = np.zeros((self.full_map_size,
                             self.full_map_size,
                             3), dtype=np.float32)

        self.full_semantic_map = np.zeros((self.full_map.shape[0], 
                                    self.full_map.shape[1],     
                                    self.object_len), dtype=np.float32)

        self.curr_full_pose = [self.map_size_cm/100.0/2.0*self.global_downscaling,
                         self.map_size_cm/100.0/2.0*self.global_downscaling, 0.]
        self.last_full = self.curr_full_pose
        self.last_sim_location = self.get_sim_location()

        # Initialize local map and pose
        self.mapper.reset_map(self.map_size_cm)
        self.local_map = np.zeros((self.local_map_size,
                             self.local_map_size,
                             3), dtype=np.float32)

        self.local_semantic_map = np.zeros((self.local_map.shape[0], 
                                    self.local_map.shape[1],     
                                    self.object_len), dtype=np.float32)
        self.curr_loc_pose = [self.map_size_cm/100.0/2.0,
                               self.map_size_cm/100.0/2.0, 0.]
        self.last_loc_gt = self.curr_loc_pose

        # Initialize slide windows map boundaries and origin
        self.local_origin = [int(self.curr_full_pose[1] * 100.0 / self.map_resolution),
                            int(self.curr_full_pose[0] * 100.0 / self.map_resolution),
                            self.curr_full_pose[2]]

        self.lmb = self.get_local_map_boundaries((self.local_origin[0], self.local_origin[1]),
                                            (self.local_map_size, self.local_map_size),
                                            (self.full_map_size,self.full_map_size))
        # Convert pose to cm and degrees for mapper
        mapper_local_pose = (self.curr_loc_pose[0]*100.0,
                          self.curr_loc_pose[1]*100.0,
                          np.deg2rad(self.curr_loc_pose[2]))

        # Update ground_truth map and explored area
        self.map, self.explored_map, self.semantic_map = \
            self.mapper.update_map(depth, semantic, mapper_local_pose)
        
        # Initialize variables
        self.scene_name = self.habitat_env.sim.config.SCENE
        self.visited_full = np.zeros((self.full_map_size,
                             self.full_map_size), dtype=np.float32)
        self.visited_gt = np.zeros(self.map.shape)
        self.collison_full_map = np.zeros((self.full_map_size,
                             self.full_map_size), dtype=np.float32)
        self.collison_map = np.zeros(self.map.shape)
        self.col_width = 1

        self.save_position()

        # obs["semantic_map"] = self.semantic_map.shape # 480*480*21
        # obs["collusion_map"] = self.map # 480*480
        # obs["explored_map"] = self.explored_map
        map_copy = self.map.copy()
        explored_map_copy = self.explored_map.copy()
        input_map = map_copy[:,:,np.newaxis]
        input_explored_map = explored_map_copy[:,:,np.newaxis]
        # print("semantic: ", self.semantic_map.shape, self.map.shape)
        map_sum = np.concatenate([input_map, self.semantic_map, input_explored_map], axis=2) 
        obs["map_sum"] = map_sum.astype(np.uint8)
        obs["curr_pose"] = np.array(
                            [(self.curr_loc_pose[1]*100 / self.map_resolution),
                            (self.curr_loc_pose[0]*100 / self.map_resolution)]).astype(np.float32)
        return obs

    def step(self, *args, **kwargs):

        self.timestep += 1

        self.last_loc_gt = np.copy(self.curr_loc_pose)

        self._previous_action = kwargs["action"]

        obs, rew, done, info = super().step(*args, **kwargs)

        # # Preprocess observations
        rgb = obs['rgb'].astype(np.uint8)
        self.obs = rgb # For visualization
        # if self.frame_width != self.env_frame_width:
        #     rgb = np.asarray(self.res(rgb))

        # state = rgb.transpose(2, 0, 1)

        depth = _preprocess_depth(obs['depth'])
        semantic = obs['semantic']
        self.semantic = semantic
        # self.object_ind = obs["objectgoal"]
        # print("object_ind: ", self.object_ind)
        # se = list(set(semantic.ravel()))
        # print(se)
        
        # print("*********** obs['depth']: ", obs['depth'])
        # print("*********** depth: ", depth)

        # Get base sensor and ground-truth pose
        dx_gt, dy_gt, do_gt = self.get_gt_pose_change()

        self.curr_loc_pose = pu.get_new_pose(self.curr_loc_pose,
                               (dx_gt, dy_gt, do_gt))
        self.curr_full_pose = pu.get_new_pose(self.curr_full_pose,
                               (dx_gt, dy_gt, do_gt))


        # Convert pose to cm and degrees for mapper
        mapper_local_pose = (self.curr_loc_pose[0]*100.0,
                          self.curr_loc_pose[1]*100.0,
                          np.deg2rad(self.curr_loc_pose[2]))

        # Update ground_truth map and explored area
        self.map, self.explored_map, self.semantic_map = \
                self.mapper.update_map(depth, semantic, mapper_local_pose)

        # print("semantic count: ", Counter(semantic.ravel()))
        # for i in range(self.semantic_map.shape[2]):
        #     se_map = list(set(np.array(self.semantic_map[:,:,i].ravel())))
        #     if len(se_map) > 1:
        #         print("self.semantic_map: ", i, Counter(np.array(self.semantic_map[:,:,i].ravel()))) # []

        # print("self._previous_action", self._previous_action)
        if self._previous_action["action"] == 1:
            x1, y1, t1 = self.last_loc_gt
            x2, y2, t2 = self.curr_loc_pose
            if abs(x1 - x2)< 0.05 and abs(y1 - y2) < 0.05:
                self.col_width += 2
                self.col_width = min(self.col_width, 9)
            else:
                self.col_width = 1

            dist = pu.get_l2_distance(x1, x2, y1, y2)
            if dist < self.collision_threshold: #Collision
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
                        r, c = int(r*100/self.map_resolution), \
                                int(c*100/self.map_resolution)
                        [r, c] = pu.threshold_poses([r, c],
                                    self.collison_map.shape)
                        self.collison_map[r,c] = 1
                        # print("collision map: ", r, c)

        # Set info
        # info['time'] = self.timestep

        self.save_position()

        map_copy = self.map.copy()
        explored_map_copy = self.explored_map.copy()
        input_map = map_copy[:,:,np.newaxis]
        input_explored_map = explored_map_copy[:,:,np.newaxis]
        # print("semantic: ", self.semantic_map.shape, self.map.shape)
        map_sum = np.concatenate([input_map, self.semantic_map, input_explored_map], axis=2) 
        # print("semantic: ", type(map_sum[0]))
        obs["map_sum"] = map_sum.astype(np.uint8)
        obs["curr_pose"] = np.array(
                            [(self.curr_loc_pose[1]*100.0 / self.map_resolution),
                            (self.curr_loc_pose[0]*100.0 / self.map_resolution)]).astype(np.float32)

        return obs, rew, done, info


    def update_full_map(self):
        # update global map
        # print("lmb: ", self.lmb)
        self.full_map[self.lmb[0]:self.lmb[1], self.lmb[2]:self.lmb[3],:] = \
            np.copy(self.mapper.map)

        self.full_semantic_map[self.lmb[0]:self.lmb[1], self.lmb[2]:self.lmb[3],:] = \
            np.copy(self.mapper.semantic_map)

        # print(self.visited_full[self.lmb[0]:self.lmb[1], self.lmb[2]:self.lmb[3]].shape)
        # print(self.visited_gt.shape)
        self.visited_full[self.lmb[0]:self.lmb[1], self.lmb[2]:self.lmb[3]] = \
            np.copy(self.visited_gt)

        self.collison_full_map[self.lmb[0]:self.lmb[1], self.lmb[2]:self.lmb[3]] = \
            np.copy(self.collison_map)

        # update boundaries
        self.local_origin = [int(self.curr_full_pose[1] * 100.0 / self.map_resolution),
                            int(self.curr_full_pose[0] * 100.0 / self.map_resolution),
                            self.curr_full_pose[2]]

        self.lmb = self.get_local_map_boundaries((self.local_origin[0], self.local_origin[1]),
                                            (self.local_map_size, self.local_map_size),
                                            (self.full_map_size,self.full_map_size))
        # print("lmb later: ", self.lmb)
        
        self.local_map = \
            self.full_map[self.lmb[0]:self.lmb[1], self.lmb[2]:self.lmb[3],:].copy()
        self.local_semantic_map = \
            self.full_semantic_map[self.lmb[0]:self.lmb[1], self.lmb[2]:self.lmb[3],:].copy()
        self.visited_gt = \
            self.visited_full[self.lmb[0]:self.lmb[1], self.lmb[2]:self.lmb[3]].copy()
        self.collison_map = \
            self.collison_full_map[self.lmb[0]:self.lmb[1], self.lmb[2]:self.lmb[3]].copy()

        # print(self.local_map.shape)
        # print(self.local_semantic_map.shape)
        self.mapper.reset_boundaries(self.local_map, self.local_semantic_map)

        self.map = self.local_map[:,:,1]
        self.map[self.map >= 0.5] = 1.0
        self.map[self.map < 0.5] = 0.0

        self.semantic_map = self.local_semantic_map
        self.semantic_map[self.semantic_map >=0.5] = 1.0
        self.semantic_map[self.semantic_map < 0.5] = 0.0

        self.explored_map = self.local_map.sum(2)
        self.explored_map[self.explored_map>1] = 1.0



        # update local pose
        # print("local_origin: ", self.local_origin)
        # print("lmb: ", self.lmb)
        self.curr_loc_pose = [
            (self.local_origin[1] - (self.lmb[2]+self.lmb[3])/2)*self.map_resolution/100.0 + self.map_size_cm/100.0/2.0, 
            (self.local_origin[0] - (self.lmb[0]+self.lmb[1])/2)*self.map_resolution/100.0 + self.map_size_cm/100.0/2.0,
            self.local_origin[2]]
        self.last_loc_gt = self.curr_loc_pose
        # print("loc: ", self.curr_loc_pose)

        return

    def get_local_map_boundaries(self, agent_loc, local_sizes, full_sizes):
        loc_r, loc_c = agent_loc
        local_w, local_h = local_sizes
        full_w, full_h = full_sizes

        if self.global_downscaling > 1:
            gx1, gy1 = loc_r - local_w // 2, loc_c - local_h // 2
            gx2, gy2 = gx1 + local_w, gy1 + local_h
            if gx1 < 0:
                gx1, gx2 = 0, local_w
            if gx2 > full_w:
                gx1, gx2 = full_w - local_w, full_w

            if gy1 < 0:
                gy1, gy2 = 0, local_h
            if gy2 > full_h:
                gy1, gy2 = full_h - local_h, full_h
        else:
            gx1, gx2, gy1, gy2 = 0, full_w, 0, full_h

        return [gx1, gx2, gy1, gy2]


    def build_mapper(self):
        params = {}
        params['frame_width'] = self.env_frame_width
        params['frame_height'] = self.env_frame_height
        params['fov'] =  self.hfov
        params['resolution'] = self.map_resolution
        params['map_size_cm'] = self.map_size_cm
        params['agent_min_z'] = self.agent_min_z
        params['agent_max_z'] = self.agent_max_z
        params['agent_height'] = self.camera_height * 100
        params['agent_view_angle'] = self.agent_view_angle
        params['du_scale'] = self.du_scale
        params['vision_range'] = self.vision_range
        params['object_len'] = self.object_len
        params['obs_threshold'] = self.obs_threshold
        self.selem = skimage.morphology.disk(5 /
                                             5)
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

    def get_local_actions(self, global_goal):
                    
        # Get last loc ground truth pose
        last_start_x, last_start_y = self.last_loc_gt[0], self.last_loc_gt[1]
        r, c = last_start_y, last_start_x
        last_start = [int(r * 100.0/self.map_resolution),
                    int(c * 100.0/self.map_resolution)]
        last_start = pu.threshold_poses(last_start, self.visited_gt.shape)

        # Get ground truth pose
        start_x_gt, start_y_gt, start_o_gt = self.curr_loc_pose
        r, c = start_y_gt, start_x_gt
        start_gt = [int(r * 100.0/self.map_resolution),
                    int(c * 100.0/self.map_resolution)]
        start_gt = pu.threshold_poses(start_gt, self.visited_gt.shape)
        
        planning_window = [0, 240, 0, 240]

        # semantic map goal
        Find_flag = False
        object_map = self.semantic_map[:,:,self.object_ind[0]-1]
        if len(object_map[object_map!=0]) > 5:
            # print("map_objectid: ", self.object_ind[0],
            #     "num: ", len(object_map[object_map!=0]))
            goal_list = np.array(np.array(object_map.nonzero()).T)
            # print("goal_list: ", goal_list.shape)

            goal_err = np.abs(goal_list - start_gt).sum(1)
            # print("goal_err: ", goal_err)

            index = np.argmin(goal_err, axis=0)
            # print("index: ", index)

            global_goal = torch.from_numpy(goal_list[index])

            Find_flag = True
            # print("global_goal: ", global_goal)
            

        goal = pu.threshold_poses(global_goal, self.map.shape)
        # print("self.map: ", self.map.shape)
        # print("self.explored_map: ", self.explored_map.shape)
        # print("start_gt: ", start_gt)
        # print("goal: ", type(goal))

        # Get short-term goal
        stg, replan = self._get_stg(self.map, self.explored_map, start_gt, np.copy(goal), planning_window)

        # print("stg: ", stg)

        # Find GT action
        gt_action = self._get_gt_action(1 - self.explored_map, 
                                        start_gt,
                                        [int(stg[0]), int(stg[1])],
                                        np.copy(goal),
                                        planning_window, start_o_gt, 
                                        Find_flag, replan)
        
        # print("gt_action: ", gt_action)
        
        # dump_dir = "habitat_baselines/dump"
        # ep_dir = '{}/episodes/{}/{}/'.format(
        #                     dump_dir, self.rank+1, self.episode_no)
        # if not os.path.exists(ep_dir):
        #     os.makedirs(ep_dir)
        # vis_grid = vu.get_colored_map(self.map,
        #                 self.collison_map,
        #                 self.visited_gt,
        #                 goal.int(),
        #                 self.explored_map,
        #                 self.explorable_map,
        #                 self.map*self.explored_map,
        #                 self.semantic_map)
        # vis_grid = np.flipud(vis_grid)

        # full_map = self.full_map[:,:,1]
        # full_map[full_map >= 0.5] = 1.0
        # full_map[full_map < 0.5] = 0.
        # self.explored_full_map = self.full_map.sum(2)
        # self.explored_full_map[self.explored_full_map>1]=1.0
        # self.full_semantic_map[self.full_semantic_map >= 0.5] = 1.0
        # self.full_semantic_map[self.full_semantic_map < 0.5] = 0.0
        # vis_full_grid = vu.get_colored_map(full_map,
        #                 self.collison_full_map,
        #                 self.visited_full,
        #                 goal.int(),
        #                 self.explored_full_map,
        #                 self.explorable_map,
        #                 full_map*self.explored_full_map,
        #                 self.full_semantic_map)
        # vis_full_grid = np.flipud(vis_full_grid)

        # vu.visualize(self.figure, self.ax, self.obs, vis_grid[:,:,::-1],
        #             (start_x_gt, start_y_gt, start_o_gt),
        #             (start_x_gt, start_y_gt, start_o_gt),
        #             dump_dir, self.rank, self.episode_no,
        #             self.timestep, self.visualize,
        #             self.print_images, self.object_name, gt_action)

        return gt_action

    

    def _get_stg(self, grid, explored, start, goal, planning_window):

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

        # print("grid: ", grid.shape) # 480*480*1
        # print(gx1, gx2, gy1, gy2, x1, x2, y1, y2)

        traversible = skimage.morphology.binary_dilation(
                        grid[x1:x2, y1:y2],
                        self.selem) != True
        traversible[self.collison_map[gx1:gx2, gy1:gy2][x1:x2, y1:y2] == 1] = 0
        traversible[self.visited_gt[gx1:gx2, gy1:gy2][x1:x2, y1:y2] == 1] = 1

        traversible[int(start[0]-x1)-1:int(start[0]-x1)+2,
                    int(start[1]-y1)-1:int(start[1]-y1)+2] = 1
        # print("traversible: ", traversible.shape)

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

        planner = FMMPlanner(traversible, 360//10)

        reachable = planner.set_goal([goal[1]-y1+1, goal[0]-x1+1])

        stg_x, stg_y = start[0] - x1 + 1, start[1] - y1 + 1
        for i in range(1):
            stg_x, stg_y, replan = planner.get_short_term_goal([stg_x, stg_y])
        if replan:
            stg_x, stg_y = start[0], start[1]
        else:
            stg_x, stg_y = stg_x + x1 - 1, stg_y + y1 - 1

        return (stg_x, stg_y), replan


    def _get_gt_action(self, grid, start, goal, g_goal, planning_window, start_o, Find_flag, replan):

        [gx1, gx2, gy1, gy2] = planning_window

        x1 = min(start[0], goal[0])
        x2 = max(start[0], goal[0])
        y1 = min(start[1], goal[1])
        y2 = max(start[1], goal[1])
        dist = pu.get_l2_distance(goal[0], start[0], goal[1], start[1])
        buf = max(5., dist)
        x1 = max(0, int(x1 - buf))
        x2 = min(grid.shape[0], int(x2 + buf))
        y1 = max(0, int(y1 - buf))
        y2 = min(grid.shape[1], int(y2 + buf))
        # print("grid: ", grid.shape)
        # print(gx1, gx2, gy1, gy2, x1, x2, y1, y2)
        path_found = False
        goal_r = 0
        while not path_found:
            traversible = skimage.morphology.binary_dilation(
                            grid[gx1:gx2, gy1:gy2][x1:x2, y1:y2],
                            self.selem) != True
            # print(grid[gx1:gx2, gy1:gy2].shape, grid[gx1:gx2, gy1:gy2][x1:x2, y1:y2].shape)
            traversible[self.visited_gt[gx1:gx2, gy1:gy2][x1:x2, y1:y2] == 1] = 1
            traversible[int(start[0]-x1)-1:int(start[0]-x1)+2,
                        int(start[1]-y1)-1:int(start[1]-y1)+2] = 1
            # print("traversible: ", traversible.shape)
            traversible[int(goal[0]-x1)-goal_r:int(goal[0]-x1)+goal_r+1,
                        int(goal[1]-y1)-goal_r:int(goal[1]-y1)+goal_r+1] = 1
            scale = 1
            planner = FMMPlanner(traversible, 360//10, scale)
            # print("traversible: ", traversible.shape)
            reachable = planner.set_goal([goal[1]-y1, goal[0]-x1])

            stg_x_gt, stg_y_gt = start[0] - x1, start[1] - y1
            for i in range(1):
                stg_x_gt, stg_y_gt, replan = \
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
        angle_st_goal = math.degrees(math.atan2(stg_x_gt - start[0],
                                                stg_y_gt - start[1]))
        angle_agent = (start_o)%360.0
        if angle_agent > 180:
            angle_agent -= 360

        relative_angle = (angle_agent - angle_st_goal)%360.0
        if relative_angle > 180:
            relative_angle -= 360

        g_dist = pu.get_l2_distance(g_goal[0], start[0], g_goal[1], start[1])

        # if Find_flag:
            # print("distance: ", g_dist)
        if (g_dist < 4.0 and Find_flag) or replan:
            gt_action = 0

        elif relative_angle > 15.:
            gt_action = 3
        elif relative_angle < -15.:
            gt_action = 2
        else:
            gt_action = 1

        return gt_action



def _preprocess_depth(depth):
    # print("depth: ", depth)
    # print("depth: ", depth.shape) # 256*256*1
    depth = depth[:, :, 0]*1
    mask2 = depth > 0.99
    depth[mask2] = 0.

    for i in range(depth.shape[1]):
        depth[:,i][depth[:,i] == 0.] = depth[:,i].max()

    mask1 = depth == 0
    depth[mask1] = np.NaN
    depth = depth*1000.
    return depth

