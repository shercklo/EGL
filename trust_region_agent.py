import torch
import torch.utils.data
import torch.utils.data.sampler
import torch.optim.lr_scheduler
import numpy as np
from tqdm import tqdm
import torch.autograd as autograd
from model_ddpg import RobustNormalizer, TrustRegion
import itertools
from agent import Agent
import os
from config import args

class TrustRegionAgent(Agent):

    def __init__(self, exp_name, env, checkpoint):
        super(TrustRegionAgent, self).__init__(exp_name, env, checkpoint)
        reward_str = "TrustRegion"
        print("Learning POLICY method using {} with TrustRegionAgent".format(reward_str))

        self.pi_trust_region = TrustRegion(self.pi_net)
        self.r_norm = RobustNormalizer()
        self.best_pi_evaluate = self.step_policy(self.pi_net.pi.detach().cpu(), to_env=False)
        self.f0 = self.best_pi_evaluate
        self.stop_con = args.stop_con
        self.no_change = 0

    def update_replay_buffer(self):

        #self.tensor_replay_reward = None
        #self.tensor_replay_policy = None

        if self.tensor_replay_reward is not None:
            pi = self.pi_trust_region.mu
            explore_policies = torch.cat(self.results['explore_policies'], dim=0)
            rewards = torch.cat(self.results['rewards'])

            in_range = torch.norm(explore_policies - pi, 1, dim=1) < self.pi_trust_region.sigma.min()
            if in_range.sum():
                explore_policies_from_buf = self.pi_trust_region.real_to_unconstrained(explore_policies[in_range])
                rewards_from_buf = rewards[in_range]
            else:
                explore_policies_from_buf = torch.FloatTensor([])
                rewards_from_buf = torch.FloatTensor([])
        else:
            explore_policies_from_buf = torch.FloatTensor([])
            rewards_from_buf = torch.FloatTensor([])

        self.frame += self.warmup_explore
        explore_policies_rand = self.exploration_rand(self.warmup_explore)
        self.step_policy(explore_policies_rand)
        rewards_rand = self.env.reward
        self.results['explore_policies'].append(self.pi_trust_region.unconstrained_to_real(explore_policies_rand))
        self.results['rewards'].append(rewards_rand)

        explore_policies = torch.cat([explore_policies_from_buf, explore_policies_rand])
        rewards = torch.cat([rewards_from_buf, rewards_rand])

        replay_size = (len(explore_policies) // self.n_explore) * self.n_explore

        explore_policies = explore_policies[-replay_size:]
        rewards = rewards[-replay_size:]

        self.tensor_replay_reward = rewards
        self.tensor_replay_policy = explore_policies

    def results_pi_update_with_explore(self):

        self.results['frame'] = self.frame
        self.results['best_observed'] = self.env.best_observed
        self.results['best_pi_evaluate'] = self.best_pi_evaluate
        _, grad = self.get_grad()
        grad = grad.cpu().numpy().reshape(1, -1)
        self.results['grad'] = grad

        pi = self.results['policies'][-1]
        real_pi = self.pi_trust_region.unconstrained_to_real(pi)
        dist_x = torch.norm(self.env.denormalize(real_pi.numpy()) - self.best_op_x, 2)
        self.results['dist_x'] = dist_x.detach().item()

        lower, upper = self.pi_trust_region.bounderies()
        bst = self.best_op_x.cpu()/5
        in_trust = (bst == torch.min(torch.max(bst, lower), upper)).min().item()
        for i in range(self.action_space):
            print("index {}: lower bound={}\tupper bound={}\tx={}\tx*={}\tin_bound {}".format(i, lower[i], upper[i], real_pi[i], bst[i], bst[i]<=upper[i] and bst[i]>=lower[i]))

        self.results['in_trust'] = in_trust

        dist_f = self.results['reward_pi_evaluate'][-1] - self.best_op_f
        self.results['dist_f'] = dist_f

        if self.algorithm_method in ['first_order', 'second_order', 'anchor']:
            val = torch.norm(self.derivative_net(self.pi_net.pi.detach()).detach(), 2).detach().item()
            self.results['grad_norm'] = val
        if self.algorithm_method in ['value', 'anchor']:
            val = self.r_norm.desquash(self.value_net(self.pi_net.pi.detach()).detach().cpu()).item()
            self.results['value'] = val

        self.results['mean_grad'] = self.mean_grad.cpu().numpy()
        self.results['divergence'] = self.divergence
        self.results['r_norm_mean'] = self.r_norm.mu.detach().item()
        self.results['r_norm_sigma'] = self.r_norm.sigma.detach().item()
        self.results['min_trust_sigma'] = self.pi_trust_region.sigma.min().item()
        self.results['no_change'] = self.no_change
        self.results['epsilon'] = self.epsilon

        self.save_results()

    def save_results(self):
        for k in self.results.keys():
            path = os.path.join(self.analysis_dir, k + '.npy')
            data_np = np.array([])
            if os.path.exists(path):
                data_np = np.load(path)

            if k in ['explore_policies']:
                policy = (torch.cat(self.results[k], dim=0)).cpu().numpy()
                if len(data_np):
                    policy = np.concatenate([data_np, policy])
                np.save(path, policy)
            elif k in ['policies']:
                policy = (torch.stack(self.results[k])).cpu().numpy()
                if len(data_np):
                    policy = np.concatenate([data_np, policy])
                np.save(path, policy)
            elif k in ['reward_pi_evaluate', 'frame_pi_evaluate']:
                rewards = self.results[k]
                if len(data_np):
                    rewards = np.concatenate([data_np, rewards])
                np.save(path, rewards)
            elif k in ['rewards']:
                rewards = torch.cat(self.results[k]).numpy()
                if len(data_np):
                    rewards = np.hstack([data_np, rewards])
                np.save(path, rewards)
            elif k in ['grad']:
                grad = self.results[k]
                if len(data_np):
                    grad = np.concatenate([data_np, grad])
                np.save(path, grad)
            else:
                data = np.array([self.results[k]])
                if len(data_np):
                    data = np.hstack([data_np, data])
                np.save(path, data)

        best_list, observed_list, _ = self.env.get_observed_and_pi_list()
        np.save(os.path.join(self.analysis_dir, 'best_list_with_explore.npy'), np.array(best_list))
        np.save(os.path.join(self.analysis_dir, 'observed_list_with_explore.npy'), np.array(best_list))

        path = os.path.join(self.analysis_dir, 'f0.npy')
        np.save(path, self.f0)

    def warmup(self):
        self.mean_grad = None
        self.reset_net()
        self.r_norm.reset()
        self.update_replay_buffer()
        self.r_norm(self.tensor_replay_reward, training=True)
        self.value_optimize(self.value_iter)

    def save_and_print_results(self):
        self.save_checkpoint(self.checkpoint, {'n': self.frame})
        self.results_pi_update_with_explore()


    def minimize(self):
        counter = -1
        self.env.reset()
        self.warmup()
        for _ in tqdm(itertools.count()):
            counter += 1
            pi_explore, reward = self.exploration_step()
            self.results['explore_policies'].append(self.pi_trust_region.unconstrained_to_real(pi_explore))
            self.results['rewards'].append(reward)

            self.value_optimize(self.value_iter)
            self.pi_optimize()

            pi = self.pi_net.pi.detach().cpu()
            pi_eval = self.step_policy(pi, to_env=False)
            self.results['reward_pi_evaluate'].append(pi_eval)
            self.results['frame_pi_evaluate'].append(self.frame)
            if pi_eval < self.best_pi_evaluate:
                self.no_change = 0
                self.best_pi_evaluate = pi_eval
            else:
                self.no_change += 1
            real_pi = self.pi_trust_region.unconstrained_to_real(pi)
            self.results['policies'].append(real_pi)

            if self.env.t or (self.best_pi_evaluate - self.best_op_f) < -1:
                self.save_and_print_results()
                yield self.results
                print("FINISHED SUCCESSFULLY - FRAME %d" % self.frame)
                break

            elif counter > self.stop_con and self.no_change > self.stop_con:
                counter = 0
                self.divergence += 1
                self.save_checkpoint(self.checkpoint, {'n': self.frame})
                self.save_and_print_results()
                self.update_best_pi()
                self.warmup()

            elif self.frame >= self.budget:
                self.save_and_print_results()
                yield self.results
                print("FAILED frame = {}".format(self.frame))
                break

            elif (self.frame % (self.printing_interval * self.n_explore)) == 0:
                self.save_and_print_results()
                yield self.results
                self.reset_result()

    def update_best_pi(self):

        reward_pi_evaluate = np.load(os.path.join(self.analysis_dir, 'reward_pi_evaluate.npy'))
        best_idx = reward_pi_evaluate.argmin()
        pi_evaluate = np.load(os.path.join(self.analysis_dir, 'policies.npy'))[best_idx]
        best_reward_pi_evaluate = reward_pi_evaluate[best_idx]

        reward_pi_explore = np.load(os.path.join(self.analysis_dir, 'rewards.npy'))
        best_idx = reward_pi_explore.argmin()
        pi_explore = np.load(os.path.join(self.analysis_dir, 'explore_policies.npy'))[best_idx]
        best_reward_pi_explore = reward_pi_explore[best_idx]

        if best_reward_pi_evaluate < best_reward_pi_explore:
            pi = torch.FloatTensor(pi_explore)
        else:
            pi = torch.FloatTensor(pi_evaluate)

        self.pi_trust_region.squeeze(pi)
        self.epsilon *= self.epsilon_factor
        self.pi_net.pi_update(self.pi_trust_region.real_to_unconstrained(pi).to(self.device))

    def value_optimize(self, value_iter):

        self.tensor_replay_reward_norm = self.r_norm(self.tensor_replay_reward).to(self.device)
        self.tensor_replay_policy_norm = self.tensor_replay_policy.to(self.device)

        len_replay_buffer = len(self.tensor_replay_reward_norm)
        self.batch = min(self.max_batch, len_replay_buffer)
        minibatches = len_replay_buffer // self.batch

        assert minibatches, 'minibatch is zero'

        if self.algorithm_method == 'first_order':
            self.first_order_method_optimize_single_ref(len_replay_buffer, minibatches, value_iter)
            #self.first_order_method_optimize(len_replay_buffer, minibatches, value_iter)
        elif self.algorithm_method in ['value']:
            self.value_method_optimize(len_replay_buffer, minibatches, value_iter)
        elif self.algorithm_method == 'second_order':
            #self.second_order_method_optimize(len_replay_buffer, minibatches, value_iter)
            self.second_order_method_optimize_single_ref(len_replay_buffer, minibatches, value_iter)
        elif self.algorithm_method == 'anchor':
            self.anchor_method_optimize(len_replay_buffer, minibatches, value_iter)
        else:
            raise NotImplementedError

    # def anchor_method_optimize(self, len_replay_buffer, minibatches, value_iter):
    #     self.value_method_optimize(len_replay_buffer, minibatches)
    #
    #     loss = 0
    #     self.derivative_net.train()
    #     self.value_net.eval()
    #     for it in itertools.count():
    #         shuffle_indexes = np.random.choice(len_replay_buffer, (minibatches, self.batch), replace=True)
    #         for i in range(minibatches):
    #             samples = shuffle_indexes[i]
    #             pi_1 = self.tensor_replay_policy_norm[samples]
    #             pi_tag_1 = self.derivative_net(pi_1)
    #             pi_2 = pi_1.detach() + torch.randn(self.batch, self.action_space).to(self.device, non_blocking=True)
    #
    #             r_1 = self.tensor_replay_reward_norm[samples]
    #             r_2 = self.value_net(pi_2).squeeze(1).detach()
    #
    #             if self.importance_sampling:
    #                 w = torch.clamp((1 / (torch.norm(pi_2 - pi_1, p=2, dim=1) + 1e-4)).flatten(), 0, 1)
    #             else:
    #                 w = 1
    #
    #             value = ((pi_2 - pi_1) * pi_tag_1).sum(dim=1)
    #             target = (r_2 - r_1)
    #
    #             self.optimizer_derivative.zero_grad()
    #             self.optimizer_pi.zero_grad()
    #             loss_q = (w * self.q_loss(value, target)).mean()
    #             loss += loss_q.detach().item()
    #             loss_q.backward()
    #             self.optimizer_derivative.step()
    #
    #         if it >= value_iter:
    #             break
    #
    #     loss /= value_iter
    #     self.results['derivative_loss'] = loss
    #     self.derivative_net.eval()


    def value_method_optimize(self, len_replay_buffer, minibatches, value_iter):
        loss = 0
        self.value_net.train()
        for _ in range(value_iter):
            shuffle_indexes = np.random.choice(len_replay_buffer, (minibatches, self.batch), replace=False)
            for i in range(minibatches):
                samples = shuffle_indexes[i]
                r = self.tensor_replay_reward_norm[samples]
                pi_explore = self.tensor_replay_policy_norm[samples]

                self.optimizer_value.zero_grad()
                self.optimizer_pi.zero_grad()
                q_value = self.value_net(pi_explore).view(-1)
                if self.spline:
                    loss_q = self.q_loss(q_value, r).sum()
                else:
                    loss_q = self.q_loss(q_value, r).mean()
                loss += loss_q.detach().item()
                loss_q.backward()
                self.optimizer_value.step()

        loss /= value_iter
        self.results['value_loss'].append(loss)
        self.value_net.eval()

    def first_order_method_optimize(self, len_replay_buffer, minibatches, value_iter):

        loss = 0

        self.derivative_net.train()
        for _ in range(value_iter):
            anchor_indexes = np.random.choice(len_replay_buffer, (minibatches, self.batch), replace=False)
            batch_indexes = anchor_indexes // self.n_explore

            for i, anchor_index in enumerate(anchor_indexes):
                ref_index = torch.LongTensor(self.batch * batch_indexes[i][:, np.newaxis] + np.arange(self.batch)[np.newaxis, :])

                r_1 = self.tensor_replay_reward_norm[anchor_index].unsqueeze(1).repeat(1, self.batch)
                r_2 = self.tensor_replay_reward_norm[ref_index]
                pi_1 = self.tensor_replay_policy_norm[anchor_index]
                pi_2 = self.tensor_replay_policy_norm[ref_index]
                pi_tag_1 = self.derivative_net(pi_1).unsqueeze(1).repeat(1, self.batch, 1)
                pi_1 = pi_1.unsqueeze(1).repeat(1, self.batch, 1)

                if self.importance_sampling:
                    w = torch.clamp((1 / (torch.norm(pi_2 - pi_1, p=2, dim=2) + 1e-4)).flatten(), 0, 1)
                else:
                    w = 1

                value = ((pi_2 - pi_1) * pi_tag_1).sum(dim=2).flatten()
                target = (r_2 - r_1).flatten()

                self.optimizer_derivative.zero_grad()
                self.optimizer_pi.zero_grad()
                if self.spline:
                    loss_q = (w * self.q_loss(value, target)).sum()
                else:
                    loss_q = (w * self.q_loss(value, target)).mean()
                loss += loss_q.detach().item()
                loss_q.backward()
                self.optimizer_derivative.step()

        loss /= value_iter
        self.results['derivative_loss'] = loss
        self.derivative_net.eval()

    def first_order_method_optimize_single_ref(self, len_replay_buffer, minibatches, value_iter):

        loss = 0
        self.derivative_net.train()
        for _ in range(value_iter):
            anchor_indexes = np.random.choice(len_replay_buffer, (minibatches, self.batch), replace=False)
            ref_indexes = np.random.randint(0, self.n_explore, size=(minibatches, self.batch))
            explore_indexes = anchor_indexes // self.n_explore

            for i, anchor_index in enumerate(anchor_indexes):
                ref_index = torch.LongTensor(self.n_explore * explore_indexes[i] + ref_indexes[i])

                r_1 = self.tensor_replay_reward_norm[anchor_index]
                r_2 = self.tensor_replay_reward_norm[ref_index]
                pi_1 = self.tensor_replay_policy_norm[anchor_index]
                pi_2 = self.tensor_replay_policy_norm[ref_index]
                pi_tag_1 = self.derivative_net(pi_1)

                if self.importance_sampling:
                    w = torch.clamp((1 / (torch.norm(pi_2 - pi_1, p=2, dim=1) + 1e-4)).flatten(), 0, 1)
                else:
                    w = 1

                value = ((pi_2 - pi_1) * pi_tag_1).sum(dim=1)
                target = (r_2 - r_1)

                self.optimizer_derivative.zero_grad()
                self.optimizer_pi.zero_grad()
                if self.spline:
                    loss_q = (w * self.q_loss(value, target)).sum()
                else:
                    loss_q = (w * self.q_loss(value, target)).mean()
                loss += loss_q.detach().item()
                loss_q.backward()
                self.optimizer_derivative.step()

        loss /= value_iter
        self.results['derivative_loss'] = loss
        self.derivative_net.eval()

    def second_order_method_optimize_single_ref(self, len_replay_buffer, minibatches, value_iter):
        loss = 0
        mid_val = True

        self.derivative_net.train()
        for _ in range(value_iter):
            anchor_indexes = np.random.choice(len_replay_buffer, (minibatches, self.batch), replace=False)
            ref_indexes = np.random.randint(0, self.n_explore, size=(minibatches, self.batch))
            explore_indexes = anchor_indexes // self.n_explore

            for i, anchor_index in enumerate(anchor_indexes):
                ref_index = torch.LongTensor(self.n_explore * explore_indexes[i] + ref_indexes[i])
                r_1 = self.tensor_replay_reward_norm[anchor_index]
                r_2 = self.tensor_replay_reward_norm[ref_index]
                pi_1 = self.tensor_replay_policy_norm[anchor_index]
                pi_2 = self.tensor_replay_policy_norm[ref_index]

                delta_pi = pi_2 - pi_1

                if mid_val:
                    mid_pi = (pi_1 + pi_2) / 2
                    mid_pi = autograd.Variable(mid_pi, requires_grad=True)
                    pi_tag_mid = self.derivative_net(mid_pi)
                    pi_tag_1 = self.derivative_net(pi_1)
                    pi_tag_mid_dot_delta = (pi_tag_mid * delta_pi).sum(dim=1)
                    gradients = autograd.grad(outputs=pi_tag_mid_dot_delta, inputs=mid_pi, grad_outputs=torch.cuda.FloatTensor(pi_tag_mid_dot_delta.size()).fill_(1.),
                                              create_graph=True, retain_graph=True, only_inputs=True)[0].detach()
                    second_order = 0.5 * (delta_pi * gradients.detach()).sum(dim=1)
                else:
                    pi_1 = autograd.Variable(pi_1, requires_grad=True)
                    pi_tag_1 = self.derivative_net(pi_1)
                    pi_tag_1_dot_delta = (pi_tag_1 * delta_pi).sum(dim=1)
                    gradients = autograd.grad(outputs=pi_tag_1_dot_delta, inputs=pi_1, grad_outputs=torch.cuda.FloatTensor(pi_tag_1_dot_delta.size()).fill_(1.),
                                              create_graph=True, retain_graph=True, only_inputs=True)[0].detach()
                    second_order = 0.5 * (delta_pi * gradients.detach()).sum(dim=1)

                if self.importance_sampling:
                    w = torch.clamp((1 / (torch.norm(pi_2 - pi_1, p=2, dim=1) + 1e-4)).flatten(), 0, 1)
                else:
                    w = 1

                value = (delta_pi * pi_tag_1).sum(dim=1)
                target = (r_2 - r_1) - second_order

                self.optimizer_derivative.zero_grad()
                self.optimizer_pi.zero_grad()
                if self.spline:
                    loss_q = (w * self.q_loss(value, target)).sum()
                else:
                    loss_q = (w * self.q_loss(value, target)).mean()
                loss += loss_q.detach().item()
                loss_q.backward()
                self.optimizer_derivative.step()

        loss /= value_iter
        self.results['derivative_loss'] = loss
        self.derivative_net.eval()

    def second_order_method_optimize(self, len_replay_buffer, minibatches, value_iter):
        loss = 0
        mid_val = True

        self.derivative_net.train()
        for _ in range(value_iter):

            shuffle_index = np.random.randint(0, self.batch, size=(minibatches,)) + np.arange(0, self.batch * minibatches, self.batch)
            r_1 = self.tensor_replay_reward_norm[shuffle_index].unsqueeze(1).repeat(1, self.batch)
            r_2 = self.tensor_replay_reward_norm.view(minibatches, self.batch)
            pi_1 = self.tensor_replay_policy_norm[shuffle_index].unsqueeze(1).repeat(1, self.batch, 1).reshape(-1,self.action_space)
            pi_2 = self.tensor_replay_policy_norm.view(minibatches * self.batch, -1)
            delta_pi = pi_2 - pi_1
            if mid_val:
                mid_pi = (pi_1 + pi_2) / 2
                mid_pi = autograd.Variable(mid_pi, requires_grad=True)
                pi_tag_mid = self.derivative_net(mid_pi)
                pi_tag_1 = self.derivative_net(pi_1)
                pi_tag_mid_dot_delta = (pi_tag_mid * delta_pi).sum(dim=1)
                gradients = autograd.grad(outputs=pi_tag_mid_dot_delta, inputs=mid_pi, grad_outputs=torch.cuda.FloatTensor(pi_tag_mid_dot_delta.size()).fill_(1.),
                                          create_graph=True, retain_graph=True, only_inputs=True)[0].detach()
                second_order = 0.5 * (delta_pi * gradients.detach()).sum(dim=1)
            else:
                pi_1 = autograd.Variable(pi_1, requires_grad=True)
                pi_tag_1 = self.derivative_net(pi_1)
                pi_tag_1_dot_delta = (pi_tag_1 * delta_pi).sum(dim=1)
                gradients = autograd.grad(outputs=pi_tag_1_dot_delta, inputs=pi_1, grad_outputs=torch.cuda.FloatTensor(pi_tag_1_dot_delta.size()).fill_(1.),
                                          create_graph=True, retain_graph=True, only_inputs=True)[0].detach()
                second_order = 0.5 * (delta_pi * gradients.detach()).sum(dim=1)

            if self.importance_sampling:
                w = torch.clamp((1 / (torch.norm(pi_2 - pi_1, p=2, dim=1) + 1e-4)).flatten(), 0, 1)
            else:
                w = 1

            value = (delta_pi * pi_tag_1).sum(dim=1).flatten()
            target = (r_2 - r_1).flatten() - second_order

            self.optimizer_derivative.zero_grad()
            self.optimizer_pi.zero_grad()
            if self.spline:
                loss_q = (w * self.q_loss(value, target)).sum()
            else:
                loss_q = (w * self.q_loss(value, target)).mean()
            loss += loss_q.detach().item()
            loss_q.backward()
            self.optimizer_derivative.step()

        loss /= value_iter
        self.results['derivative_loss'] = loss
        self.derivative_net.eval()

    def step_policy(self, policy, to_env=True):
        policy = policy.cpu()
        policy = self.pi_trust_region.unconstrained_to_real(policy)
        if to_env:
            self.env.step_policy(policy)
        else:
            return self.env.f(policy)

    def exploration_step(self):
        self.frame += self.n_explore
        pi_explore = self.exploration(self.n_explore)
        self.step_policy(pi_explore)
        rewards = self.env.reward
        if self.best_explore_update:
            best_explore = rewards.argmin()
            self.pi_net.pi_update(pi_explore[best_explore].to(self.device))

        self.r_norm(rewards, training=True)

        self.tensor_replay_reward = torch.cat([self.tensor_replay_reward, rewards])[-self.replay_memory_size:]
        self.tensor_replay_policy = torch.cat([self.tensor_replay_policy, pi_explore])[-self.replay_memory_size:]

        #self.print_robust_norm_params()
        return pi_explore, rewards

    def get_evaluation_function(self, policy, target):
        upper = max((self.pi_trust_region.mu + self.pi_trust_region.sigma).numpy(), 1-1e-5)
        lower = min((self.pi_trust_region.mu - self.pi_trust_region.sigma).numpy(), -1)
        policy = np.clip(policy, a_min=lower, a_max=upper)

        target = torch.FloatTensor(target)

        self.value_net.eval()
        batch = 1024
        value = []
        grads_norm = []
        for i in range(0, policy.shape[0], batch):
            from_index = i
            to_index = min(i + batch, policy.shape[0])
            policy_tensor = torch.FloatTensor(policy[from_index:to_index])
            policy_tensor = self.pi_trust_region.real_to_unconstrained(policy_tensor).to(self.device)
            policy_tensor = autograd.Variable(policy_tensor, requires_grad=True)
            target_tensor = torch.FloatTensor(target[from_index:to_index]).to(self.device)
            q_value = self.value_net(policy_tensor).view(-1)
            value.append(q_value.detach().cpu().numpy())

            if self.spline:
                loss_q = self.q_loss(q_value, target_tensor).sum()
            else:
                loss_q = self.q_loss(q_value, target_tensor).mean()

            grads = autograd.grad(outputs=loss_q, inputs=policy_tensor, grad_outputs=torch.cuda.FloatTensor(loss_q.size()).fill_(1.),
                                      create_graph=True, retain_graph=True, only_inputs=True)[0].detach()
            grads_norm.append(torch.norm(torch.clamp(grads.view(-1, self.action_space), -1, 1), p=2, dim=1).cpu().numpy())

        value = np.hstack(value)
        grads_norm = np.hstack(grads_norm)
        pi = self.pi_net.pi.detach().cpu()
        pi_value = self.value_net(self.pi_net.pi).detach().cpu().numpy()
        pi_with_grad = pi - self.pi_lr*self.get_grad().cpu()

        return value, self.pi_trust_region.unconstrained_to_real(pi).numpy(), np.array(pi_value), self.pi_trust_region.unconstrained_to_real(pi_with_grad).numpy(), grads_norm, self.r_norm(target).numpy()

    def get_grad_norm_evaluation_function(self, policy, f):
        upper = max((self.pi_trust_region.mu + self.pi_trust_region.sigma).numpy(), 1 - 1e-5)
        lower = min((self.pi_trust_region.mu - self.pi_trust_region.sigma).numpy(), -1)
        policy = np.clip(policy, a_min=lower, a_max=upper)

        f = torch.FloatTensor(f)
        self.derivative_net.eval()
        policy_tensor = torch.FloatTensor(policy)
        policy_tensor = self.pi_trust_region.real_to_unconstrained(policy_tensor).to(self.device)
        policy_diff = policy_tensor[1:]-policy_tensor[:-1]
        policy_diff_norm = policy_diff / (torch.norm(policy_diff, p=2, dim=1, keepdim=True) + 1e-5)
        grad_direct = (policy_diff_norm * self.derivative_net(policy_tensor[:-1]).detach()).sum(dim=1).cpu().numpy()
        pi = self.pi_net.pi.detach().cpu()
        pi_grad = self.derivative_net(self.pi_net.pi).detach()
        pi_with_grad = pi - self.pi_lr*pi_grad.cpu()
        pi_grad_norm = torch.norm(pi_grad).cpu()
        return grad_direct, self.pi_trust_region.unconstrained_to_real(pi).numpy(), pi_grad_norm, self.pi_trust_region.unconstrained_to_real(pi_with_grad).numpy(), self.r_norm(f).numpy()

