import numpy as np
from nose2 import tools

from rllab.env.box2d.car_parking_env import CarParkingEnv
from rllab.env.box2d.cartpole_env import CartpoleEnv
from rllab.env.box2d.cartpole_swingup_env import CartpoleSwingupEnv
from rllab.env.box2d.double_pendulum_env import DoublePendulumEnv
from rllab.env.box2d.mountain_car_env import MountainCarEnv
from rllab.env.grid_world_env import GridWorldEnv
from rllab.env.identification_env import IdentificationEnv
from rllab.env.proxy_env import ProxyEnv

simple_env_classes = [
    GridWorldEnv,
    CartpoleEnv,
    CarParkingEnv,
    CartpoleSwingupEnv,
    DoublePendulumEnv,
    MountainCarEnv,
]
envs = [cls() for cls in simple_env_classes]
envs.append(
    ProxyEnv(envs[0])
)
envs.append(
    IdentificationEnv(CartpoleEnv, {})
)


@tools.params(*envs)
def test_env(env):
    print "Testing", env.__class__
    ob_space = env.observation_space
    act_space = env.action_space
    ob = env.reset()
    assert ob_space.contains(ob)
    a = act_space.sample()
    assert act_space.contains(a)
    res = env.step(a)
    assert ob_space.contains(res.observation)
    assert np.isscalar(res.reward)
    env.render()

