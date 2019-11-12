import time
import os
import sys
import numpy as np
from tensorboardX import SummaryWriter

from config import consts, args, DirsAndLocksSingleton
from single_agent import BBOAgent

from logger import logger
from distutils.dir_util import copy_tree

import scipy.optimize  # to define the solver to be benchmarked

class Experiment(object):

    def __init__(self, logger_file, problem):

        # parameters
        self.action_space = args.action_space
        dirs = os.listdir(consts.outdir)

        self.load_model = args.load_last_model
        self.load_last = args.load_last_model
        self.resume = args.resume
        self.problem = problem
        self.problem_index = self.problem.index

        temp_name = "%s_%s_%s_bbo_%s" % (args.game, args.algorithm, args.identifier, args.problem_index)
        self.exp_name = ""
        if self.load_model:
            if self.resume >= 0:
                for d in dirs:
                    if "%s_%04d_" % (temp_name, self.resume) in d:
                        self.exp_name = d
                        self.exp_num = self.resume
                        break
            elif self.resume == -1:

                ds = [d for d in dirs if temp_name in d]
                ns = np.array([int(d.split("_")[-3]) for d in ds])
                self.exp_name = ds[np.argmax(ns)]
            else:
                raise Exception("Non-existing experiment")

        if not self.exp_name:
            # count similar experiments
            n = max([-1] + [int(d.split("_")[-3]) for d in dirs if temp_name in d]) + 1
            self.exp_name = "%s_%04d_%s" % (temp_name, n, consts.exptime)
            self.load_model = False
            self.exp_num = n

        # init experiment parameters
        self.dirs_locks = DirsAndLocksSingleton(self.exp_name)

        self.root = self.dirs_locks.root

        # set dirs
        self.tensorboard_dir = os.path.join(self.dirs_locks.tensorboard_dir, problem.id)
        self.checkpoints_dir = self.dirs_locks.checkpoints_dir
        self.results_dir = self.dirs_locks.results_dir
        self.code_dir = self.dirs_locks.code_dir
        self.analysis_dir = self.dirs_locks.analysis_dir
        self.checkpoint = self.dirs_locks.checkpoint

        if self.load_model:
            logger.info("Resuming existing experiment")
            with open(os.path.join(self.root, "logger"), "a") as fo:
                fo.write("%s resume\n" % logger_file)
        else:
            logger.info("Creating new experiment")
            # copy code to dir
            copy_tree(os.path.abspath("."), self.code_dir)

            # write args to file
            filename = os.path.join(self.root, "args.txt")
            with open(filename, 'w') as fp:
                fp.write('\n'.join(sys.argv[1:]))

            with open(os.path.join(self.root, "logger"), "a") as fo:
                fo.write("%s\n" % logger_file)

        # initialize tensorboard writer
        if args.tensorboard:
            self.writer = SummaryWriter(log_dir=self.tensorboard_dir, comment=args.identifier)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if args.tensorboard:
            self.writer.export_scalars_to_json(os.path.join(self.tensorboard_dir, "all_scalars.json"))
            self.writer.close()

    def bbo(self):

        agent = BBOAgent(self.exp_name, self.problem, checkpoint=self.checkpoint)

        n_explore = args.batch
        if args.debug:
            player = agent.find_min_temp(n_explore)
        else:
            player = agent.find_min(n_explore)

        for n, bbo_results in (enumerate(player)):
            beta = bbo_results['policies'][-1]
            beta_explore = np.average(bbo_results['explore_policies'][-1], axis=0)
            value = -np.average(bbo_results['q_value'][-1])
            reward = np.average(bbo_results['rewards'][-1])
            best_observe = bbo_results['best_observed'][-1]
            grads = bbo_results['grads'][-1]
            beta_evaluate = bbo_results['beta_evaluate'][-1]
            loss = bbo_results['q_loss'][-1]
            r_mean = bbo_results['reward_mean'][-1]

            if not n % 20:
                logger.info("-------------------------------- {} ---------------------------------".format(n))
                logger.info("Problem index     :{}\t\t\tDim: {}".format(self.problem_index, self.action_space))
                logger.info("Actions statistics: |\t mean = %.3f \t value = %.3f \t reward = %.3f \t q_loss = %.3f|" % (r_mean, value, reward, loss))
                logger.info("Best observe      : |\t %f \t \tBeta_evaluate: = %f|" % (best_observe, beta_evaluate))

                with np.printoptions(formatter={'float': '{: 0.3f}'.format}):
                    logger.info("Beta        :" + str(beta))
                    logger.info("Beta_explore:" + str(beta_explore))
                    logger.info("Grads       :" + str(grads))

            # log to tensorboard
            if args.tensorboard:

                self.writer.add_scalars('evaluation/value_reward', {'value': value, 'reward': reward}, n)
                self.writer.add_scalars('evaluation/beta_evaluate_observe', {'evaluate': beta_evaluate, 'best': best_observe}, n)

                for i in range(len(beta)):
                    self.writer.add_scalars('evaluation/beta_' + str(i), {'beta': beta[i], 'explore': beta_explore[i]}, n)
                    self.writer.add_scalar('evaluation/grad_' + str(i), grads[i], n)

                if hasattr(agent, "beta_net"):
                    self.writer.add_histogram("evaluation/beta_net", agent.beta_net.clone().cpu().data.numpy(), n, 'fd')
                if hasattr(agent, "value_net"):
                    for name, param in agent.value_net.named_parameters():
                        self.writer.add_histogram("evaluation/value_net/%s" % name, param.clone().cpu().data.numpy(), n, 'fd')

        print("End evaluation")

    def bbo_with_grads(self):

        agent = BBOAgent(self.exp_name, self.problem, checkpoint=self.checkpoint)

        n_explore = args.batch
        player = agent.find_min_grad_eval(n_explore)

        for n, bbo_results in (enumerate(player)):
            beta = bbo_results['policies'][-1]
            beta_explore = np.average(bbo_results['explore_policies'][-1], axis=0)
            value = bbo_results['value'][-1]
            target = bbo_results['target'][-1]
            best_observe = bbo_results['best_observed'][-1]
            grads = bbo_results['grads'][-1].flatten()
            beta_evaluate = bbo_results['beta_evaluate'][-1]
            loss = bbo_results['q_loss'][-1]

            if not n % 20:
                logger.info("-------------------------------- {} ---------------------------------".format(n))
                logger.info("Problem index     :{}\t\t\tDim: {}".format(self.problem_index, self.action_space))
                logger.info("Actions statistics: |\t value = %.3f \t target = %.3f \t q_loss = %.3f|" % (value, target, loss))
                logger.info("Best observe      : |\t %f \t \tBeta_evaluate: = %f|" % (best_observe, beta_evaluate))

                with np.printoptions(formatter={'float': '{: 0.3f}'.format}):
                    logger.info("Beta        :" + str(beta))
                    logger.info("Beta_explore:" + str(beta_explore))
                    logger.info("Grads       :" + str(grads))

            # log to tensorboard
            if args.tensorboard:
                self.writer.add_scalars('evaluation/value_reward', {'value': value, 'target': target}, n)
                self.writer.add_scalars('evaluation/beta_evaluate_observe', {'evaluate': beta_evaluate, 'best': best_observe}, n)

                for i in range(len(beta)):
                    self.writer.add_scalars('evaluation/beta_' + str(i), {'beta': beta[i], 'explore': beta_explore[i]}, n)
                    self.writer.add_scalar('evaluation/grad_' + str(i), grads[i], n)

                if hasattr(agent, "beta_net"):
                    self.writer.add_histogram("evaluation/beta_net", agent.beta_net.clone().cpu().data.numpy(), n, 'fd')
                if hasattr(agent, "value_net"):
                    for name, param in agent.value_net.named_parameters():
                        self.writer.add_histogram("evaluation/value_net/%s" % name, param.clone().cpu().data.numpy(), n, 'fd')

        print("End evaluation")
