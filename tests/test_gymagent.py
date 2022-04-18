import sys
import os

import gym

from gym.wrappers import TimeLimit
from omegaconf import OmegaConf
from salina import instantiate_class, get_arguments, get_class, Workspace
from salina.agents import Agents, TemporalAgent

import torch
import torch.nn as nn

from salina.agent import Agent
from salina.agents.gymb import AutoResetGymAgent, NoAutoResetGymAgent


def build_backbone(sizes, activation):
    layers = []
    for j in range(len(sizes) - 2):
        layers += [nn.Linear(sizes[j], sizes[j + 1]), activation]
    return layers


def build_mlp(sizes, activation, output_activation=nn.Identity()):
    layers = []
    for j in range(len(sizes) - 1):
        act = activation if j < len(sizes) - 2 else output_activation
        layers += [nn.Linear(sizes[j], sizes[j + 1]), act]
    return nn.Sequential(*layers)


def _index(tensor_3d, tensor_2d):
    """
    This function is used to index a 3d tensors using a 2d tensor
    """
    x, y, z = tensor_3d.size()
    t = tensor_3d.reshape(x * y, z)
    tt = tensor_2d.reshape(x * y)
    v = t[torch.arange(x * y), tt]
    v = v.reshape(x, y)
    return v


class ProbAgent(Agent):
    def __init__(self, state_dim, hidden_layers, n_action):
        super().__init__(name="prob_agent")
        self.model = build_mlp([state_dim] + list(hidden_layers) + [n_action], activation=nn.ReLU())

    def forward(self, t, **kwargs):
        observation = self.get(("env/env_obs", t))
        scores = self.model(observation)
        action_probs = torch.softmax(scores, dim=-1)
        if torch.any(torch.isnan(action_probs)):
            print("Nan Here")
        self.set(("action_probs", t), action_probs)
        entropy = torch.distributions.Categorical(action_probs).entropy()
        self.set(("entropy", t), entropy)


class ActionAgent(Agent):
    def __init__(self):
        super().__init__()

    def forward(self, t, stochastic, **kwargs):
        probs = self.get(("action_probs", t))
        if stochastic:
            action = torch.distributions.Categorical(probs).sample()
        else:
            action = probs.argmax(1)

        self.set(("action", t), action)

        
class VAgent(Agent):
    def __init__(self, state_dim, hidden_layers):
        super().__init__()
        self.model = build_mlp([state_dim] + list(hidden_layers) + [1], activation=nn.ReLU())

    def forward(self, t, **kwargs):
        observation = self.get(("env/env_obs", t))
        critic = self.model(observation).squeeze(-1)
        self.set(("critic", t), critic)


class Logger:
    def __init__(self, cfg):
        self.logger = instantiate_class(cfg.logger)

    def add_log(self, log_string, loss, epoch):
        self.logger.add_scalar(log_string, loss.item(), epoch)

    # Log losses
    def log_losses(self, epoch, critic_loss, entropy_loss, a2c_loss):
        self.add_log("critic_loss", critic_loss, epoch)
        self.add_log("entropy_loss", entropy_loss, epoch)
        self.add_log("a2c_loss", a2c_loss, epoch)


class AutoResetEnvAgent(AutoResetGymAgent):
    # Create the environment agent
    # This agent implements N gym environments with auto-reset
    def __init__(self, cfg, n_envs):
        super().__init__(get_class(cfg.gym_env), get_arguments(cfg.gym_env), n_envs)
        env = instantiate_class(cfg.gym_env)
        env.seed(cfg.algorithm.seed)
        self.observation_space = env.observation_space
        self.action_space = env.action_space
        del env


class NoAutoResetEnvAgent(NoAutoResetGymAgent):
    # Create the environment agent
    # This agent implements N gym environments without auto-reset
    def __init__(self, cfg, n_envs):
        super().__init__(get_class(cfg.gym_env), get_arguments(cfg.gym_env), n_envs)
        env = instantiate_class(cfg.gym_env)
        env.seed(cfg.algorithm.seed)
        self.observation_space = env.observation_space
        self.action_space = env.action_space
        del env


# Create the A2C Agent
def create_a2c_agent(cfg, train_env_agent, eval_env_agent):
    observation_size, n_actions = train_env_agent.get_obs_and_actions_sizes()
    param_agent = ProbAgent(observation_size, cfg.algorithm.architecture.hidden_size, n_actions)
    action_agent = ActionAgent()
    tr_agent = Agents(train_env_agent, param_agent, action_agent)
    ev_agent = Agents(eval_env_agent, param_agent, action_agent)

    critic_agent = VAgent(observation_size, cfg.algorithm.architecture.hidden_size)

    # Get an agent that is executed on a complete workspace
    train_agent = TemporalAgent(tr_agent)
    eval_agent = TemporalAgent(ev_agent)
    train_agent.seed(cfg.algorithm.seed)
    return train_agent, eval_agent, param_agent, critic_agent


def make_gym_env(max_episode_steps, env_name):
    return TimeLimit(gym.make(env_name), max_episode_steps=max_episode_steps)


# Configure the optimizer over the a2c agent
def setup_optimizers(cfg, action_agent, critic_agent):
    optimizer_args = get_arguments(cfg.optimizer)
    parameters = nn.Sequential(action_agent, critic_agent).parameters()
    optimizer = get_class(cfg.optimizer)(parameters, **optimizer_args)
    return optimizer


def compute_critic_loss(cfg, reward, done, critic):
    # Compute temporal difference
    target = reward[1:] + cfg.algorithm.discount_factor * critic[1:].detach() * (1 - done[1:].float())
    td = target - critic[:-1]

    # Compute critic loss
    td_error = td ** 2
    critic_loss = td_error.mean()
    return critic_loss, td


def compute_actor_loss_continuous(action_logp, td):
    a2c_loss = action_logp[:-1] * td.detach()
    return a2c_loss.mean()


def compute_actor_loss_discrete(action_probs, action, td):
    action_logp = _index(action_probs, action).log()
    a2c_loss = action_logp[:-1] * td.detach()
    return a2c_loss.mean()


def run_a2c(cfg, max_grad_norm=0.5):
    # 1)  Build the  logger
    logger = Logger(cfg)

    # 2) Create the environment agent
    train_env_agent = AutoResetEnvAgent(cfg, n_envs=cfg.algorithm.n_envs)
    eval_env_agent = NoAutoResetEnvAgent(cfg, n_envs=cfg.algorithm.nb_evals)

    # 3) Create the A2C Agent
    a2c_agent, eval_agent, param_agent, critic_agent = create_a2c_agent(cfg, train_env_agent, eval_env_agent)

    # 4) Create the temporal critic agent to compute critic values over the workspace
    tcritic_agent = TemporalAgent(critic_agent)

    # 5) Configure the workspace to the right dimension
    # Note that no parameter is needed to create the workspace.
    # In the training loop, calling the agent() and critic_agent()
    # will take the workspace as parameter
    train_workspace = Workspace()  # Used for training

    # 6) Configure the optimizer over the a2c agent
    optimizer = setup_optimizers(cfg, param_agent, critic_agent)
    nb_steps = 0
    tmp_steps = 0

    # 7) Training loop
    for epoch in range(cfg.algorithm.max_epochs):
        # Execute the agent in the workspace
        if epoch > 0:
            train_workspace.zero_grad()
            train_workspace.copy_n_last_steps(1)
            a2c_agent(train_workspace, t=1, n_steps=cfg.algorithm.n_steps - 1, stochastic=True)
        else:
            a2c_agent(train_workspace, t=0, n_steps=cfg.algorithm.n_steps, stochastic=True)

        # Compute the critic value over the whole workspace
        tcritic_agent(train_workspace, n_steps=cfg.algorithm.n_steps)
        nb_steps += cfg.algorithm.n_steps * cfg.algorithm.n_envs

        critic, done, reward, action = train_workspace["critic", "env/done", "env/reward", "action"]
        if train_env_agent.is_continuous_action():
            # Get relevant tensors (size are timestep x n_envs x ....)
            action_logp = train_workspace["action_logprobs"]
            # Compute critic loss
            critic_loss, td = compute_critic_loss(cfg, reward, done, critic)
            a2c_loss = compute_actor_loss_continuous(action_logp, td)
        else:
            action_probs = train_workspace["action_probs"]
            critic_loss, td = compute_critic_loss(cfg, reward, done, critic)
            a2c_loss = compute_actor_loss_discrete(action_probs, action, td)

        # Compute entropy loss
        # entropy_loss = torch.distributions.Categorical(action_probs).entropy().mean()
        entropy_loss = torch.mean(train_workspace['entropy'])

        # Store the losses for tensorboard display
        logger.log_losses(nb_steps, critic_loss, entropy_loss, a2c_loss)

        # Compute the total loss
        loss = (
            -cfg.algorithm.entropy_coef * entropy_loss
            + cfg.algorithm.critic_coef * critic_loss
            - cfg.algorithm.a2c_coef * a2c_loss
        )

        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(param_agent.parameters(), max_grad_norm)
        optimizer.step()

        if nb_steps - tmp_steps > cfg.algorithm.eval_interval:
            tmp_steps = nb_steps
            eval_workspace = Workspace()  # Used for evaluation
            eval_agent(eval_workspace, t=0, stop_variable="env/done", stochastic=False)
            rewards = eval_workspace["env/cumulated_reward"][-1]
            mean = rewards.mean()
            logger.add_log("reward", mean, nb_steps)
            print(f"epoch: {epoch}, reward: {mean }")


params = {
    "logger": {"classname": "salina.logger.TFLogger",
               "log_dir": "./tmp",
               "verbose": False,
               # "cache_size": 10000,
               "every_n_seconds": 10},
    "algorithm": {
        "seed": 2,
        "n_envs": 8,
        "n_steps": 200,
        "eval_interval": 2000,
        "nb_evals": 1,
        "max_epochs": 1000,
        "discount_factor": 0.95,
        "entropy_coef": 0.001,
        "critic_coef": 1.0,
        "a2c_coef": 0.1,
        "architecture": {"hidden_size": [25, 25]},
    },
    "gym_env": {"classname": "__main__.make_gym_env",
                "env_name": "CartPole-v1",
                "max_episode_steps": 500},
    "optimizer": {"classname": "torch.optim.Adam",
                  "lr": 0.01},
}

if __name__ == "__main__":
    # with autograd.detect_anomaly():
    sys.path.append(os.getcwd())
    config = OmegaConf.create(params)
    torch.manual_seed(config.algorithm.seed)
    run_a2c(config)
