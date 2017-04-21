import matplotlib

matplotlib.use('Agg')
import os
import os.path as osp
import multiprocessing
import random
import numpy as np
import tensorflow as tf
import tflearn
from collections import OrderedDict

from sandbox.young_clgan.envs.maze.maze_evaluate_old import test_and_plot_policy  # TODO: unify interface w/ Init
from sandbox.young_clgan.envs.maze.point_maze_env import PointMazeEnv
from sandbox.young_clgan.logging import HTMLReport
from sandbox.young_clgan.logging import format_dict
from sandbox.young_clgan.logging.visualization import save_image, plot_labeled_samples, \
    plot_line_graph

os.environ['THEANO_FLAGS'] = 'floatX=float32,device=cpu'
os.environ['CUDA_VISIBLE_DEVICES'] = ''

from rllab.algos.trpo import TRPO
from rllab.baselines.linear_feature_baseline import LinearFeatureBaseline
from rllab.envs.normalized_env import normalize
from rllab.policies.gaussian_mlp_policy import GaussianMLPPolicy

from sandbox.young_clgan.envs.base import UniformListGoalGenerator, FixedGoalGenerator, update_env_goal_generator, \
    generate_onpolicy_goals
from sandbox.young_clgan.goal.generator import StateGAN

from sandbox.young_clgan.logging.logger import ExperimentLogger
from sandbox.young_clgan.goal.evaluator import convert_label, label_goals, evaluate_goals
from sandbox.young_clgan.state.utils import StateCollection

from rllab.misc import logger

from sandbox.young_clgan.utils import initialize_parallel_sampler

initialize_parallel_sampler()

EXPERIMENT_TYPE = osp.basename(__file__).split('.')[0]


def run_task(v):
    random.seed(v['seed'])
    np.random.seed(v['seed'])

    # Log performance of randomly initialized policy with FIXED goal [0.1, 0.1]
    logger.log("Initializing report and plot_policy_reward...")
    log_dir = logger.get_snapshot_dir()  # problem with logger module here!!
    report = HTMLReport(osp.join(log_dir, 'report.html'), images_per_row=3)

    report.add_header("{}".format(EXPERIMENT_TYPE))
    report.add_text(format_dict(v))

    tf_session = tf.Session()

    env = normalize(PointMazeEnv(
        goal_generator=FixedGoalGenerator([0.1, 0.1]),
        reward_dist_threshold=v['reward_dist_threshold'],
        # reward_dist_threshold=v['reward_dist_threshold'] * 0.1,
        # append_goal=False,
        indicator_reward=v['indicator_reward'],
        terminal_eps=v['terminal_eps'],
        only_feas=v['only_feas'],
    ))

    policy = GaussianMLPPolicy(
        env_spec=env.spec,
        hidden_sizes=(64, 64),
        # Fix the variance since different goals will require different variances, making this parameter hard to learn.
        learn_std=v['learn_std'],
        adaptive_std=v['adaptive_std'],
        std_hidden_sizes=(16, 16),  # this is only used if adaptive_std is true!
        output_gain=v['output_gain'],
        init_std=v['policy_init_std'],
    )

    baseline = LinearFeatureBaseline(env_spec=env.spec)

    # initialize all logging arrays on itr0
    all_mean_rewards = []
    all_success = []
    outer_iter = 0
    n_traj = 3 if v['indicator_reward'] else 1
    sampling_res = 2
    # logger.log('Generating the Initial Heatmap...')
    # avg_rewards, avg_success, heatmap = test_and_plot_policy(policy, env, max_reward=v['max_reward'],
    #                                                          sampling_res=sampling_res, n_traj=n_traj)
    # reward_img = save_image()

    # mean_rewards = np.mean(avg_rewards)
    # success = np.mean(avg_success)

    # all_mean_rewards.append(mean_rewards)
    # all_success.append(success)
    #
    # with logger.tabular_prefix('Outer_'):
    #     logger.record_tabular('iter', outer_iter)
    #     logger.record_tabular('MeanRewards', mean_rewards)
    #     logger.record_tabular('Success', success)
    # # logger.dump_tabular(with_prefix=False)
    #
    # report.add_image(
    #     reward_img,
    #     'policy performance\n itr: {} \nmean_rewards: {} \nsuccess: {}'.format(
    #         outer_iter, all_mean_rewards[-1],
    #         all_success[-1]
    #     )
    # )

    # GAN
    logger.log("Instantiating the GAN...")
    gan_configs = {key[4:]: value for key, value in v.items() if 'GAN_' in key}
    for key, value in gan_configs.items():
        if value is tf.train.AdamOptimizer:
            gan_configs[key] = tf.train.AdamOptimizer(gan_configs[key + '_stepSize'])
        if value is tflearn.initializations.truncated_normal:
            gan_configs[key] = tflearn.initializations.truncated_normal(stddev=gan_configs[key + '_stddev'])

    final_gen_loss = 11
    k = -1
    while final_gen_loss > 10:
        k += 1
        gan = StateGAN(
            goal_size=v['goal_size'],
            evaluater_size=v['num_labels'],
            goal_range=v['goal_range'],
            goal_center=v['goal_center'],
            goal_noise_level=v['goal_noise_level'],
            generator_layers=v['gan_generator_layers'],
            discriminator_layers=v['gan_discriminator_layers'],
            noise_size=v['gan_noise_size'],
            generator_max_iters=v['gan_generator_max_iters'],
            discriminator_max_iters=v['gan_discriminator_max_iters'],
            generator_min_loss=v['gan_generator_min_loss'],
            discriminator_min_loss=v['gan_discriminator_min_loss'],
            tf_session=tf_session,
            configs=gan_configs,
        )
        logger.log("pretraining the GAN...")
        if v['smart_init']:
            on_policy_goals = generate_onpolicy_goals(env, policy, goal_range=v['goal_range'],
                                                      center=v['goal_center'], horizon=v['horizon'])
            dis_loss, gen_loss = gan.pretrain(on_policy_goals,
                                              outer_iters=v['gan_outer_iters'],
                                              generator_max_iters=v['gan_generator_max_iters'] + k,
                                              discriminator_max_iters=v['gan_discriminator_max_iters'] - k * 10,
                                              )
            final_gen_loss = gen_loss
            img = plot_labeled_samples(on_policy_goals, sample_classes=np.zeros(len(on_policy_goals), dtype=int),
                                       colors=['k'], text_labels={0: 'training states'}, limit=v['goal_range'],
                                       center=v['goal_center'])
            report.add_image(img,
                             "States used to train the NEXT gan.\nerror at the end of {}th trial:\n {}gen,\n {}disc".format(
                                 k, gen_loss, dis_loss), width=500)
        else:
            dis_loss, gen_loss = gan.pretrain_uniform()
            final_gen_loss = 0

    # log first samples form the GAN
    initial_goals, _ = gan.sample_states_with_noise(v['num_new_goals'])
    logger.log("Labeling the goals")
    labels = label_goals(
        initial_goals, env, policy, v['horizon'],
        min_reward=v['min_reward'],
        max_reward=v['max_reward'],
        old_rewards=None,
        improvement_threshold=v['improvement_threshold'],
        n_traj=n_traj)

    logger.log("Converting the labels")
    goal_classes, text_labels = convert_label(labels)

    logger.log("Plotting the labeled samples")
    total_goals = labels.shape[0]
    goal_class_frac = OrderedDict()  # this needs to be an ordered dict!! (for the log tabular)
    for k in text_labels.keys():
        frac = np.sum(goal_classes == k) / total_goals
        logger.record_tabular('GenGoal_frac_' + text_labels[k], frac)
        goal_class_frac[text_labels[k]] = frac

    img = plot_labeled_samples(
        samples=initial_goals, sample_classes=goal_classes, text_labels=text_labels, limit=v['goal_range'],
        center=v['goal_center'],
        # '{}/sampled_goals_{}.png'.format(log_dir, outer_iter),  # if i don't give the file it doesn't save
    )
    summary_string = ''
    for key, value in goal_class_frac.items():
        if value > 0:
            summary_string += key + ' frac: ' + str(value) + '\n'
    summary_string += "\nGANerror at the end of {}th outerItr:\n {}gen,\n {}disc".format(v['gan_outer_iters'], gen_loss, dis_loss)
    report.add_image(img, 'itr: {}\nLabels of generated goals:\n{}'.format(outer_iter, summary_string), width=500)
    report.save()
    report.new_row()

    # save here to be in same order as outer_iters
    # logger.record_tabular("GAN_dis_itr", len(dis_loss))
    # logger.record_tabular("GAN_gen_itr", len(gen_loss))
    logger.record_tabular("GAN_dis_final_loss", dis_loss)
    logger.record_tabular("GAN_gen_final_loss", gen_loss)

    all_goals = StateCollection(distance_threshold=v['coll_eps'])

    for outer_iter in range(1, v['outer_iters']):

        logger.log("Outer itr # %i" % outer_iter)
        # Sample GAN
        logger.log("Sampling goals from the GAN")
        raw_goals, _ = gan.sample_states_with_noise(v['num_new_goals'])

        if v['replay_buffer'] and outer_iter > 0 and all_goals.size > 0:
            old_goals = all_goals.sample(v['num_old_goals'], replay_noise=v['replay_noise'])
            goals = np.vstack([raw_goals, old_goals])
        else:
            goals = raw_goals

        logger.log("Evaluating goals before training")
        rewards_before = None
        if v['num_labels'] == 3:
            rewards_before = evaluate_goals(goals, env, policy, v['horizon'], n_traj=n_traj,
                                            n_processes=multiprocessing.cpu_count())

        with ExperimentLogger(log_dir, snapshot_mode='last', hold_outter_log=True):
            logger.log("Updating the environment goal generator")
            update_env_goal_generator(
                env,
                UniformListGoalGenerator(
                    goals.tolist()
                )
            )

            logger.log("Training the algorithm")
            algo = TRPO(
                env=env,
                policy=policy,
                baseline=baseline,
                batch_size=v['pg_batch_size'],
                max_path_length=v['horizon'],
                n_itr=v['inner_iters'],
                discount=v['discount'],
                step_size=0.01,
                plot=False,
                gae_lambda=v['gae_lambda'])

            algo.train()

        logger.log('Generating the Heatmap...')
        avg_rewards, avg_success, heatmap = test_and_plot_policy(policy, env,  # max_reward=v['max_reward'],
                                                                 sampling_res=sampling_res, n_traj=n_traj,
                                                                 visualize=False)
        reward_img = save_image()

        mean_rewards = np.mean(avg_rewards)
        success = np.mean(avg_success)

        all_mean_rewards.append(mean_rewards)
        all_success.append(success)

        with logger.tabular_prefix('Outer_'):
            logger.record_tabular('iter', outer_iter)
            logger.record_tabular('MeanRewards', mean_rewards)
            logger.record_tabular('Success', success)
        # logger.dump_tabular(with_prefix=False)

        report.add_image(
            reward_img,
            'policy performance\n itr: {} \nmean_rewards: {}\nsuccess: {}'.format(
                outer_iter, all_mean_rewards[-1],
                all_success[-1]
            )
        )
        report.save()

        logger.log("Labeling the goals")
        labels = label_goals(
            goals, env, policy, v['horizon'],
            min_reward=v['min_reward'],
            max_reward=v['max_reward'],
            old_rewards=rewards_before,
            improvement_threshold=v['improvement_threshold'],
            n_traj=n_traj)

        # append new goals to list of all goals (replay buffer): Not the low reward ones!!
        filtered_raw_goals = [goal for goal, label in zip(goals, labels) if label[0] == 1]
        all_goals.append(filtered_raw_goals)

        logger.log("Converting the labels")
        goal_classes, text_labels = convert_label(labels)

        logger.log("Plotting the labeled samples")
        total_goals = labels.shape[0]
        goal_class_frac = OrderedDict()  # this needs to be an ordered dict!! (for the log tabular)
        for k in text_labels.keys():
            frac = np.sum(goal_classes == k) / total_goals
            logger.record_tabular('GenGoal_frac_' + text_labels[k], frac)
            goal_class_frac[text_labels[k]] = frac

        img = plot_labeled_samples(
            samples=goals, sample_classes=goal_classes, text_labels=text_labels, limit=v['goal_range'],
            center=v['goal_center'],
            # '{}/sampled_goals_{}.png'.format(log_dir, outer_iter),  # if i don't give the file it doesn't save
        )
        summary_string = ''
        for key, value in goal_class_frac.items():
            if value > 0:
                summary_string += key + ' frac: ' + str(value) + '\n'
        summary_string += "\nGANerror at the end of itrs:\n {}gen,\n {}disc".format(gen_loss, dis_loss)
        report.add_image(img, 'itr: {}\nLabels of generated goals:\n{}'.format(outer_iter, summary_string), width=500)

        # ###### extra for deterministic:
        # logger.log("Labeling the goals deterministic")
        # with policy.set_std_to_0():
        #     labels_det = label_goals(
        #         goals, env, policy, v['horizon'],
        #         min_reward=v['min_reward'],
        #         max_reward=v['max_reward'],
        #         old_rewards=rewards_before,
        #         improvement_threshold=v['improvement_threshold'],
        #         n_traj=n_traj, n_processes=1)
        #
        # logger.log("Converting the labels")
        # goal_classes, text_labels = convert_label(labels_det)
        #
        # logger.log("Plotting the labeled samples")
        # total_goals = labels_det.shape[0]
        # goal_class_frac = OrderedDict()  # this needs to be an ordered dict!! (for the log tabular)
        # for k in text_labels.keys():
        #     frac = np.sum(goal_classes == k) / total_goals
        #     logger.record_tabular('GenGoal_frac_' + text_labels[k], frac)
        #     goal_class_frac[text_labels[k]] = frac
        #
        # img = plot_labeled_samples(
        #     samples=goals, sample_classes=goal_classes, text_labels=text_labels, limit=v['goal_range'] + 1,
        #     # '{}/sampled_goals_{}.png'.format(log_dir, outer_iter),  # if i don't give the file it doesn't save
        # )
        # summary_string = ''
        # for key, value in goal_class_frac.items():
        #     summary_string += key + ' frac: ' + str(value) + '\n'
        # report.add_image(img, 'itr: {}\nLabels of generated goals with DETERMINISTIC policy:\n{}'.format(outer_iter, summary_string), width=500)

        ######  try single label for good goals
        if v['num_labels'] == 1:
            labels = np.logical_and(labels[:, 0], labels[:, 1]).astype(int).reshape((-1, 1))

        logger.log("Training the GAN")
        dis_loss, gen_loss = gan.train(goals, labels)

        # logger.record_tabular("GAN_dis_itr", len(dis_loss) * gan.gan.configs['print_iteration'])
        # logger.record_tabular("GAN_gen_itr", len(gen_loss))
        logger.record_tabular("GAN_dis_final_loss", dis_loss)
        logger.record_tabular("GAN_gen_final_loss", gen_loss)

        logger.dump_tabular(with_prefix=False)
        report.save()
        report.new_row()

    img = plot_line_graph(
        osp.join(log_dir, 'mean_rewards.png'),
        range(v['outer_iters']), all_mean_rewards
    )
    report.add_image(img, 'Mean rewards', width=500)

    report.save()
