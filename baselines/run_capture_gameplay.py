import sys
import multiprocessing
import os.path as osp
import gym
from collections import defaultdict
import tensorflow as tf
import numpy as np

from baselines.common.vec_env.vec_frame_stack import VecFrameStack
from baselines.common.cmd_util import common_arg_parser, parse_unknown_args, make_vec_env, make_env
from baselines.common.tf_util import get_session
from baselines import bench, logger
from importlib import import_module

from baselines.common.vec_env.vec_normalize import VecNormalize

# variability_RL imports
import time
from scipy.stats import sem
from statistics import stdev
from PIL import Image

from baselines.common import atari_wrappers, retro_wrappers
#

# variability RL functions
# Hot patch atari env so we can get the score
# This is exactly the same, except we put the result of act into the info
from gym.envs.atari import AtariEnv
def hotpatch_step(self, a):
    reward = 0.0
    action = self._action_set[a]
    # Since reward appears to be incremental, dynamically add an instance variable to track.
    # So there's a __getattribute__ function, but no __hasattribute__ function? Bold, Python.
    try:
        self.score = self.score
    except AttributeError:
        self.score = 0.0

    if isinstance(self.frameskip, int):
        num_steps = self.frameskip
    else:
        num_steps = self.np_random.randint(self.frameskip[0], self.frameskip[1])
    
    for _ in range(num_steps):
        reward += self.ale.act(action)
    ob = self._get_obs()
    done = self.ale.game_over()
    # Update score

    score = self.score
    self.score = 0.0 if done else self.score + reward
    # Return score as part of info
    return ob, reward, done, {"ale.lives": self.ale.lives(), "score": score}

AtariEnv.step = hotpatch_step
#



try:
    from mpi4py import MPI
except ImportError:
    MPI = None

try:
    import pybullet_envs
except ImportError:
    pybullet_envs = None

try:
    import roboschool
except ImportError:
    roboschool = None

_game_envs = defaultdict(set)
for env in gym.envs.registry.all():
    # TODO: solve this with regexes
    env_type = env._entry_point.split(':')[0].split('.')[-1]
    _game_envs[env_type].add(env.id)

# reading benchmark names directly from retro requires
# importing retro here, and for some reason that crashes tensorflow
# in ubuntu
_game_envs['retro'] = {
    'BubbleBobble-Nes',
    'SuperMarioBros-Nes',
    'TwinBee3PokoPokoDaimaou-Nes',
    'SpaceHarrier-Nes',
    'SonicTheHedgehog-Genesis',
    'Vectorman-Genesis',
    'FinalFight-Snes',
    'SpaceInvaders-Snes',
}


def train(args, extra_args):
    env_type, env_id = get_env_type(args.env)
    print('env_type: {}'.format(env_type))

    total_timesteps = int(args.num_timesteps)
    seed = args.seed

    learn = get_learn_function(args.alg)
    alg_kwargs = get_learn_function_defaults(args.alg, env_type)
    alg_kwargs.update(extra_args)

    env = build_env(args)

    if args.network:
        alg_kwargs['network'] = args.network
    else:
        if alg_kwargs.get('network') is None:
            alg_kwargs['network'] = get_default_network(env_type)

    print('Training {} on {}:{} with arguments \n{}'.format(args.alg, env_type, env_id, alg_kwargs))

    model = learn(
        env=env,
        seed=seed,
        total_timesteps=total_timesteps,
        **alg_kwargs
    )

    return model, env


def build_env(args):
    ncpu = multiprocessing.cpu_count()
    if sys.platform == 'darwin': ncpu //= 2
    nenv = args.num_env or ncpu
    alg = args.alg
    #seed = args.seed

    # set the same environment seed for variability experiments
    seed = 9874


    env_type, env_id = get_env_type(args.env)

    if env_type in {'atari', 'retro'}:
        if alg == 'deepq':
            env = make_env(env_id, env_type, seed=seed, wrapper_kwargs={'frame_stack': True})
        elif alg == 'trpo_mpi':
            env = make_env(env_id, env_type, seed=seed)
        else:
            frame_stack_size = 4
            env = make_vec_env(env_id, env_type, nenv, seed, gamestate=args.gamestate, reward_scale=args.reward_scale)
            env = VecFrameStack(env, frame_stack_size)

    else:
       config = tf.ConfigProto(allow_soft_placement=True,
                               intra_op_parallelism_threads=1,
                               inter_op_parallelism_threads=1)
       config.gpu_options.allow_growth = True
       get_session(config=config)

       env = make_vec_env(env_id, env_type, args.num_env or 1, seed, reward_scale=args.reward_scale)

       if env_type == 'mujoco':
           env = VecNormalize(env)

    return env


def get_env_type(env_id):
    if env_id in _game_envs.keys():
        env_type = env_id
        env_id = [g for g in _game_envs[env_type]][0]
    else:
        env_type = None
        for g, e in _game_envs.items():
            if env_id in e:
                env_type = g
                break
        assert env_type is not None, 'env_id {} is not recognized in env types'.format(env_id, _game_envs.keys())

    return env_type, env_id


def get_default_network(env_type):
    if env_type == 'atari':
        return 'cnn'
    else:
        return 'mlp'

def get_alg_module(alg, submodule=None):
    submodule = submodule or alg
    try:
        # first try to import the alg module from baselines
        alg_module = import_module('.'.join(['baselines', alg, submodule]))
    except ImportError:
        # then from rl_algs
        alg_module = import_module('.'.join(['rl_' + 'algs', alg, submodule]))

    return alg_module


def get_learn_function(alg):
    return get_alg_module(alg).learn


def get_learn_function_defaults(alg, env_type):
    try:
        alg_defaults = get_alg_module(alg, 'defaults')
        kwargs = getattr(alg_defaults, env_type)()
    except (ImportError, AttributeError):
        kwargs = {}
    return kwargs



def parse_cmdline_kwargs(args):
    '''
    convert a list of '='-spaced command-line arguments to a dictionary, evaluating python objects when possible
    '''
    def parse(v):

        assert isinstance(v, str)
        try:
            return eval(v)
        except (NameError, SyntaxError):
            return v

    return {k: parse(v) for k,v in parse_unknown_args(args).items()}



def main():
    # configure logger, disable logging in child MPI processes (with rank > 0)
    arg_parser = common_arg_parser()
    args, unknown_args = arg_parser.parse_known_args()
    extra_args = parse_cmdline_kwargs(unknown_args)

    if MPI is None or MPI.COMM_WORLD.Get_rank() == 0:
        rank = 0
        logger.configure()
    else:
        logger.configure(format_strs=[])
        rank = MPI.COMM_WORLD.Get_rank()


    n_trials = 100

    path_stem = extra_args['load_path']
    model_id = path_stem.split("/")[1]

    model_exists = osp.exists(extra_args['load_path'])
    data_exists = osp.exists('data/model_scores_'+model_id+'.tsv')
    if model_exists and not data_exists:
        
        with tf.Graph().as_default(): 
            model, env = train(args, extra_args)
            env.close()

            if args.save_path is not None and rank == 0:
                save_path = osp.expanduser(args.save_path)
                model.save(save_path)

            if args.play:
                saved_steps = []
                logger.log("Running trained model",model_id)
                env = build_env(args)
                obs = env.reset()
                turtle = atari_wrappers.get_turtle(env)
                scores = []
                session_scores = set()
                num_games = 0
                # This is a hack to get the starting screen, which throws an error in ALE for amidar
                num_steps = -1

                while num_games < n_trials:
                    num_steps += 1
                    fname = 'gameplay/screen'+str(num_steps)+'.png'
                    #turtle.ale.saveScreenPNG(fname.encode('ascii'))

                    actions = model.step(obs)[0]
                    saved_steps.append(actions)

                    num_lives = turtle.ale.lives()
                    obs, _, done, info = env.step(actions)
                    #env.render()
                    #time.sleep(1.0/60.0)
                    done = num_lives == 1 and done 

                    if isinstance(info, list) or isinstance(info, tuple):
                        session_scores.add(np.average([d['score'] for d in info]))
                    elif isinstance(info, dict):
                        session_scores.add(['score'])
                    else:
                        session_scores.add(-1)

                    if done:
                        num_games += 1
                        num_steps = 0
                        score = max(session_scores)
                        scores.append(score)
                        session_scores = set()

                        print("game %s: %s" % (num_games, score))
                        obs = env.reset()
                        session_scores = set()

                print("Avg score: %f" % np.average(scores))
                print("Median score: %f" % np.median(scores))
                print("Std error score: %f" % sem(scores))
                print("Std dev score: %f" % stdev(scores))
                env.close()

                with open('data/model_scores_'+model_id+'.tsv', 'w') as fp:
                   for n in scores:
                       print('\t'.join([model_id, str(n)]), file=fp)
                with open('gameplay/saved_steps_'+model_id+'.tsv', 'w') as fp:
                    print('model\tstep\taction') 
                    for i, st in enumerate(saved_steps): 
                        acts = [str(a) for a in st]
                        acts = ('\t').join(acts)
                        print(('\t').join([model_id, str(i), acts]), file=fp)

if __name__ == '__main__':
    main()
