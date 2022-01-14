#
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
#

import copy
import time

import hydra
import torch
import torch.nn as nn

import salina.rl.functional as RLF
from salina import Agent, Workspace, instantiate_class
from salina.agents import Agents, TemporalAgent
from salina.agents.brax import AutoResetBraxAgent,NoAutoResetBraxAgent
from salina.agents.gyma import AutoResetGymAgent,NoAutoResetGymAgent
from salina.logger import TFLogger
from salina_examples.rl.ppo_brax.agents import make_brax_env,make_gym_env,make_env
import numpy as np
import random
from salina_cl.algorithms.optimizers.tools import compute_time_unit
from salina.agents.remote import NRemoteAgent

def clip_grad(parameters, grad):
    return (
        torch.nn.utils.clip_grad_norm_(parameters, grad)
        if grad > 0
        else torch.Tensor([0.0])
    )
def _state_dict(agent, device):
    sd = agent.state_dict()
    for k, v in sd.items():
        sd[k] = v.to(device)
    return sd

def ppo_train(action_agent, critic_agent, env_agent,logger, cfg_ppo):
    time_unit=None
    if cfg_ppo.stop_criterion=="time":
        time_unit=compute_time_unit(cfg_ppo.device)
        logger.message("Time unit is "+str(time_unit)+" seconds.")

    action_agent.set_name("action")
    acquisition_agent = TemporalAgent(Agents(env_agent, action_agent)).to(cfg_ppo.acquisition_device)
    acquisition_workspace=Workspace()
    if cfg_ppo.n_processes>1:
        acquisition_agent,acquisition_workspace=NRemoteAgent.create(acquisition_agent, num_processes=cfg_ppo.n_processes, time_size=cfg_ppo.n_timesteps, n_steps=1, replay=False,train=True)
    acquisition_agent.seed(cfg_ppo.seed)

    train_agent = Agents(action_agent, critic_agent).to(cfg_ppo.learning_device)
    optimizer_policy = torch.optim.Adam(action_agent.parameters(), lr=cfg_ppo.lr_policy)
    optimizer_critic = torch.optim.Adam(critic_agent.parameters(), lr=cfg_ppo.lr_critic)

    # === Running algorithm
    epoch = 0
    iteration = 0
    n_interactions = 0

    _epoch_start_time = time.time()
    is_training=True
    while is_training:
        # Acquisition of trajectories
        for a in acquisition_agent.get_by_name("action"):
            a.load_state_dict(_state_dict(action_agent, cfg_ppo.acquisition_device))

        acquisition_workspace.zero_grad()
        if epoch > 0: acquisition_workspace.copy_n_last_steps(1)
        acquisition_agent.train()
        acquisition_agent(
            acquisition_workspace,
            t=1 if epoch > 0 else 0,
            n_steps=cfg_ppo.n_timesteps - 1
            if epoch > 0
            else cfg_ppo.n_timesteps,
            replay=False,
            train=True,
            action_std=cfg_ppo.action_std,
        )
        workspace=Workspace(acquisition_workspace).to(cfg_ppo.learning_device)
        n_interactions+=(workspace.time_size()-1)*workspace.batch_size()
        logger.add_scalar("monitor/n_interactions", n_interactions, epoch)

        # Log cumulated reward of training trajectories
        d=workspace["env/done"]
        if d.any():
            r=workspace["env/cumulated_reward"][d].mean().item()
            logger.add_scalar("monitor/avg_training_reward",r,epoch)

            if "env/success" in list(workspace.keys()):
                r=workspace["env/success"][d].mean().item()
                logger.add_scalar("monitor/success",r,epoch)

        workspace.zero_grad()
        workspace.set_full("old_action_logprobs",workspace["action_logprobs"].detach())

        #Building mini workspaces
        #Learning for cfg.algorithm.update_epochs epochs
        miniworkspaces=[]
        _stb=time.time()
        for _ in range(cfg_ppo.n_mini_batches):
            miniworkspace=workspace.sample_subworkspace(cfg_ppo.n_times_per_minibatch,cfg_ppo.n_envs_per_minibatch,cfg_ppo.n_timesteps_per_minibatch)
            miniworkspaces.append(miniworkspace)
        _etb=time.time()
        logger.add_scalar("monitor/minibatches_building_time",_etb-_stb,epoch)

        #Learning on batches
        for miniworkspace in miniworkspaces:
            # === Update policy
            train_agent(
                miniworkspace,
                t=None,
                replay=True,
                train=True,
                action_std=cfg_ppo.action_std,
            )
            critic, done, reward = miniworkspace["critic", "env/done", "env/reward"]
            old_action_lp = miniworkspace["old_action_logprobs"]
            reward = reward * cfg_ppo.reward_scaling
            gae = RLF.gae(
                critic,
                reward,
                done,
                cfg_ppo.discount_factor,
                cfg_ppo.gae,
            ).detach()
            action_lp = miniworkspace["action_logprobs"]
            ratio = action_lp - old_action_lp
            ratio = ratio.exp()
            ratio = ratio[:-1]
            clip_adv = (
                torch.clamp(
                    ratio,
                    1 - cfg_ppo.clip_ratio,
                    1 + cfg_ppo.clip_ratio,
                )
                * gae
            )
            loss_policy = -(torch.min(ratio * gae, clip_adv)).mean()

            td0 = RLF.temporal_difference(
                critic, reward, done, cfg_ppo.discount_factor
            )
            loss_critic = (td0 ** 2).mean()
            optimizer_critic.zero_grad()
            optimizer_policy.zero_grad()
            (loss_policy + loss_critic).backward()
            n = clip_grad(action_agent.parameters(), cfg_ppo.clip_grad)
            optimizer_policy.step()
            optimizer_critic.step()
            logger.add_scalar("monitor/grad_norm_policy", n.item(), iteration)
            logger.add_scalar("loss/policy", loss_policy.item(), iteration)
            logger.add_scalar("loss/critic", loss_critic.item(), iteration)
            logger.add_scalar("monitor/grad_norm_critic", n.item(), iteration)
            iteration += 1
        epoch += 1

        if cfg_ppo.stop_criterion=="epochs":
            is_training=epoch<cfg_ppo.max_epochs
        elif cfg_ppo.stop_criterion=="steps":
            is_training=n_interactions<cfg_ppo.max_steps
        elif cfg_ppo.stop_criterion=="time":
            is_training=time.time()-_epoch_start_time<cfg_ppo.time_limit*time_unit
        else:
            assert False
    r={"n_epochs":epoch,"training_time":time.time()-_epoch_start_time,"n_interactions":n_interactions}
    if cfg_ppo.n_processes>1: acquisition_agent.close()
    return r
