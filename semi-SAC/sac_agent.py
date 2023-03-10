import torch
from torch.optim import Adam, lr_scheduler
import numpy as np
import gym
import copy
from buffer import SemiMDPReplayBuffer
from sac_model import SACActor
from td3_model import DoubleCritic, DoubleCritics
from utils import freeze, unfreeze
from typing import Any, List, Callable, Dict

from gym_lifter.envs.fab.action_set import action2operation

class SACAgent:
    def __init__(self,
                 dimS,
                 env,
                 nA,
                 num_layer,
                 action_map: Callable[..., List[int]],
                 gamma=0.99,
                 pi_lr=3e-4,
                 q_lr=3e-4,
                 alpha_lr=3e-4,
                 polyak=1e-3,
                 alpha=0.2,
                 adjust_entropy=False,
                 target_entropy=-6.,
                 hidden1=128,
                 hidden2=128,
                 hidden3=128,
                 buffer_size=1000000,
                 batch_size=128,
                 device='cpu',
                 render=False,
                 dueling=False):
        self.env = env
        self.dimS = dimS
        self.nA = nA
        self.gamma = gamma
        self.pi_lr = pi_lr
        self.q_lr = q_lr
        self.polyak = polyak
        # attributes for automating entropy adjustment
        self.adjust_entropy = adjust_entropy
        if adjust_entropy:
            self.target_entropy = target_entropy
            self.log_alpha = torch.tensor([0.], requires_grad=True, device=device)

            self.alpha_optimizer = Adam([self.log_alpha], lr=alpha_lr)

            self.alpha = torch.exp(self.log_alpha)
            # self.alpha_scheduler = lr_scheduler.LambdaLR(self.alpha_optimizer, lr_lambda=lambda epoch: 0.99995 * epoch)
        else:
            # if the temperature parameter is not adjusted automatically, we set it to a fixed value
            self.alpha = alpha

        self.batch_size = batch_size
        # networks definition
        # pi : actor network, Q : 2 critic network
        self.pi = SACActor(num_layer, dimS, nA, hidden1, hidden2, hidden3).to(device)
        if dueling:
            self.Q = DoubleCritics(num_layer, dimS, nA, hidden1, hidden2, hidden3).to(device)
        else:
            self.Q = DoubleCritic(num_layer, dimS, nA, hidden1, hidden2, hidden3).to(device)

        # target networks
        self.target_Q = copy.deepcopy(self.Q).to(device)

        freeze(self.target_Q)
        self.action_map = action_map
        self.buffer = SemiMDPReplayBuffer(dimS, limit=buffer_size)

        self.Q_optimizer = Adam(self.Q.parameters(), lr=self.q_lr)
        self.pi_optimizer = Adam(self.pi.parameters(), lr=self.pi_lr)
        #.alpha_optimizer = Adam(self.pi.parameters(), lr=alpha_lr)

        # self.Q_scheduler = lr_scheduler.LambdaLR(self.Q_optimizer, lr_lambda=lambda epoch: 0.99995 * epoch)
        # self.pi_scheduler = lr_scheduler.LambdaLR(self.pi_optimizer, lr_lambda=lambda epoch: 0.99995 * epoch)
        self.device = device
        self.render = render

        return


    def get_action_rule(self, state, rule = 0):
        #Rule = { 0: SJF-TFP, 1: LWF-TFP, 2: Random }

        possible_actions = self.action_map(state)  # return a set of indices instead of a mask vector
        possible_actions = random.sample(possible_actions, len(possible_actions))



        #Longest Waiting First(LWF) + Two-Forks Priority(TFP)
        if rule ==1:
            max_wait=-10.
            waiting_time=-10.
            a= None
            for action in possible_actions:

                if action == self.nA - 1:
                    operation = None
                else:
                    operation = self.env.action2operation[action]

                if operation is None:
                    waiting_time = 0.
                else:
                    floor, pos, low_up, load_unload = operation
                    operation_time = int(abs(pos - self.env.rack_pos)) * 0.5 + 2. + 3.
                    bonus = np.random.rand() ### Priority value to Two-Forks
                    if load_unload == 0:  # LOAD
                        if low_up == 0:
                            waiting_time = self.env.fab.t - self.env.fab.layers[self.env.fab.pos2label[pos][0]].cmd_time
                        elif low_up == 1:
                            waiting_time = self.env.fab.t - self.env.fab.layers[self.env.fab.pos2label[pos][1]].cmd_time
                        else:
                            waiting_time = max(self.env.fab.t - self.env.fab.layers[self.env.fab.pos2label[pos][0]].cmd_time,
                                               self.env.fab.t - self.env.fab.layers[self.env.fab.pos2label[pos][1]].cmd_time) + bonus
                    if load_unload == 1:  # UNLOAD
                        if low_up == 0:
                            waiting_time = self.env.fab.t - self.env.fab.rack.lower_fork.CMD_RACK
                        elif low_up == 1:
                            waiting_time = self.env.fab.t - self.env.fab.rack.upper_fork.CMD_RACK
                        else:
                            waiting_time = max(self.env.fab.t - self.env.fab.rack.lower_fork.CMD_RACK,
                                               self.env.fab.t  - self.env.fab.rack.upper_fork.CMD_RACK) + bonus


                if max_wait <= waiting_time:

                    max_wait = waiting_time
                    a = action




        #Shortest Job First (SJF) + Two-Forks Priority(TWF)
        elif rule ==0 :
            possible_actions = random.sample( possible_actions, len(possible_actions) )
            min_t = 1e+8
            for action in possible_actions:
                bonus = 0
                if action == self.nA-1:
                    operation = None
                else:
                    operation = self.env.action2operation[action]

                if operation is None:
                    operation_time = 1000.
                else:
                    floor, pos, low_up, load_unload = operation
                    if low_up == 2:
                        bonus = - np.random.rand() ### Priority value to Two-Forks
                    operation_time = int(abs(pos - self.env.rack_pos)) * 0.5 + 2. + 3. + bonus
                    # operation_time = distance_matrix[pos, self.env.rack_pos] + 3.
                    # operations.append(operation)

                if min_t >= operation_time:
                    min_t = operation_time
                    a = action

        ##Random
        else:
            prob = np.zeros_like(possible_actions) * 0.
            for i, action in enumerate(possible_actions):
                if action == self.nA-1:
                    prob[i] = 1e-8
                else:
                    prob[i] = 1.
                    operation = self.env.action2operation[action]
                    floor, pos, low_up, load_unload = operation
                    if low_up == 2:
                        prob[i] += 5.

            prob /= np.sum(prob)
            a = np.random.choice(possible_actions, p = prob)



        return a



    def get_action(self, state):
        admissible_actions: List[int] = self.action_map(state)
        state = torch.tensor(state, dtype=torch.float).to(self.device)
        with torch.no_grad():
            p = self.pi(state)
        p = p.cpu().detach().numpy()

        p_new = (p + 1e-4) * mask_mul(admissible_actions, num_actions=self.nA)

        p_new = p_new / np.sum(p_new)
        action = np.random.choice(self.nA, p=p_new)
        return action

    def target_update(self):
        for params, target_params in zip(self.Q.parameters(), self.target_Q.parameters()):
            target_params.data.copy_(self.polyak * params.data + (1.0 - self.polyak) * target_params.data)
        return

    def train(self, iter):
        
        device = self.device
        batch = self.buffer.sample_batch(batch_size=self.batch_size)
        epsilon = 1e-4
        states = batch['state']
        # m = np.vstack([mask_add(self.action_map(states[i])) for i in range(self.batch_size)])
        # m = torch.tensor(m, dtype=torch.float).to(self.device)
        m2 = np.vstack([mask_mul(self.action_map(states[i]), num_actions=self.nA) for i in range(self.batch_size)])
        m2 = torch.tensor(m2, dtype=torch.float).to(self.device)

        next_states = batch['next_state']
        m3 = np.vstack([mask_mul(self.action_map(next_states[i]), num_actions=self.nA) for i in range(self.batch_size)])
        m3 = torch.tensor(m3, dtype=torch.float).to(self.device)

        # unroll batch
        states_tensor = torch.tensor(states, dtype=torch.float).to(device)
        actions = torch.tensor(batch['action'], dtype=torch.long).to(device)
        rewards = torch.tensor(batch['reward'], dtype=torch.float).to(device)
        next_states = torch.tensor(batch['next_state'], dtype=torch.float).to(device)
        d = torch.tensor(batch['done'], dtype=torch.float).to(device)
        dt = torch.tensor(batch['dt'], dtype=torch.float).to(device)

        with torch.no_grad():
            # TODO : invalid action filtering
            
            
            probs = self.pi(next_states)

            probs_new = m3 * (probs + 1e-4)
            probs_new = probs_new / torch.sum(probs_new, dim=1, keepdim=True)
            # clipped double-Q target
            target_q1, target_q2 = self.target_Q(next_states)       # Q_1(s^\prime, \cdot), Q_2(s^\prime, \cdot)
            # a_next = torch.unsqueeze(torch.max(target_q1 + m, 1)[1], 1)

            target_q = torch.min(target_q1, target_q2)
            v_next = torch.sum(probs_new * target_q, dim=1, keepdim=True)
            # \mathbb{E}_{a \sim \pi(\cdot \vert s)}Q^\pi (s^\prime, a^\prime)
            # v1_next = torch.sum(probs_new * target_q1, dim=1, keepdim=True)
            # v2_next = torch.sum(probs_new * target_q2, dim=1, keepdim=True)
            # v_next = torch.min(v1_next, v2_next)

            # entropy of policy
            log_probs = torch.log(probs_new + epsilon)
            H = -torch.sum(probs_new * log_probs, dim=1, keepdim=True)

            """
            target_q1, target_q2 = self.target_Q(next_states)  # Q_1(s^\prime, \cdot), Q_2(s^\prime, \cdot)
            # a_next = torch.unsqueeze(torch.max(target_q1 + m, 1)[1], 1)

            # \mathbb{E}_{a \sim \pi(\cdot \vert s)}Q^\pi (s^\prime, a^\prime)
            target_q = torch.min(target_q1, target_q2)
            v_next = torch.sum(probs * target_q, dim=1, keepdim=True)
            # v1_next = torch.sum(probs * target_q1, dim=1, keepdim=True)
            # v2_next = torch.sum(probs * target_q2, dim=1, keepdim=True)
            # v_next = torch.min(v1_next, v2_next)

            log_probs = torch.log(probs + epsilon)
            H = torch.sum(probs * log_probs, dim=1, keepdim=True)
            """
            # semi-MDP target construction
            target = rewards + (self.gamma ** dt) * (1. - d) * (v_next + self.alpha * H)

        # out1, out2 = self.Q(states).gather(1, actions)
        q1, q2 = self.Q(states_tensor)
        out1 = q1.gather(1, actions)
        out2 = q2.gather(1, actions)
        Q_loss1 = torch.mean((out1 - target)**2)
        Q_loss2 = torch.mean((out2 - target)**2)
        Q_loss = Q_loss1 + Q_loss2

        self.Q_optimizer.zero_grad()
        Q_loss.backward()
        self.Q_optimizer.step()

        # actor loss
        # here we use normalized probability which considers a set of admissible actions at each state

        probs = self.pi(states_tensor)

        probs_new = m2 * (probs + 1e-4)
        probs_new = probs_new / torch.sum(probs_new, dim=1, keepdim=True)
        log_probs = torch.log(probs_new + 1e-7)
        freeze(self.Q)
        q1, q2 = self.Q(states_tensor)
        q = torch.min(q1, q2)

        # pi_loss = torch.mean(self.alpha * log_probs - q)
        pi_loss = torch.mean(probs_new * (self.alpha * log_probs - q))
        # pi_loss = -torch.mean(probs_new *  q)
        # print(pi_loss.item())
        self.pi_optimizer.zero_grad()
        pi_loss.backward()
        self.pi_optimizer.step()

        if self.adjust_entropy:
            alpha_loss = -torch.mean(self.log_alpha * (log_probs + self.target_entropy).detach())

            self.alpha_optimizer.zero_grad()
            alpha_loss.backward()
            self.alpha_optimizer.step()
            self.alpha = torch.exp(self.log_alpha)

        unfreeze(self.Q)
        # self.alpha_scheduler.step()
        # self.Q_scheduler.step()
        # self.pi_scheduler.step()

        self.target_update()

        return

    def eval(self,
             test_env: gym.Env,
             T=14400,
             eval_num: int = 3
             ) -> Dict[str, Any]:
        """
        evaluation of agent
        during evaluation, agent execute noiseless actions
        """
        print('evaluating on 24 hrs data...', end=' ')
        reward_log = np.zeros(eval_num)
        num_log = np.zeros(eval_num, dtype=int)
        total_log = np.zeros(eval_num, dtype=int)
        max_flow_log = np.zeros(eval_num)
        avg_flow_log = np.zeros(eval_num)
        max_wait_log = np.zeros(eval_num)
        avg_wait_log = np.zeros(eval_num)

        for ep in range(eval_num):
            state = test_env.reset()
            if len(state) == 2:
                state = state[0]

            step_count = 0
            ep_reward = 0.
            t = 0.
            # done = False
            info = None
            # while not done:
            while t < T:
                # half hr evaluation
                if self.render and ep == 0:
                    test_env.render()

                action = self.get_action(state)#, waiting_time=test_env.waiting_time)  # noiseless evaluation
                next_state, reward, done,  _, info = test_env.step(action)

                step_count += 1
                state = next_state
                ep_reward += self.gamma ** t * reward
                t = info['elapsed_time']
            if self.render and ep == 0:
                    test_env.close()
            # save carried quantity at the end of the episode
            log = test_env.operation_log
            carried = log['carried']
            total = log['total']
            max_flow_log[ep] = log['max_flow_time']
            avg_flow_log[ep] = log['average_flow_time']
            max_wait_log[ep] = log['max_waiting_time']
            avg_wait_log[ep] = log['average_waiting_time']
            reward_log[ep] = ep_reward
            num_log[ep] = carried
            total_log[ep] = sum(total)
            if self.render and ep == 0:
                test_env.close()
        reward_avg = np.mean(reward_log)
        num_avg = np.mean(num_log)
        avg_ratio = np.mean(num_log / total_log)
        max_flow = np.mean(max_flow_log)
        avg_flow = np.mean(avg_flow_log)
        max_wait = np.mean(max_wait_log)
        avg_wait = np.mean(avg_wait_log)

        # display
        print('average reward : {:.4f} || carried (ratio) : {} ({:.2f}%)'.format(reward_avg, num_avg, 100. * avg_ratio))
        print('max_flow_time : {:.4f} / avg_flow_time : {:.4f}'.format(max_flow, avg_flow))
        print('max_waitng_time : {:.4f} / avg_waiting_time : {:.4f}'.format(max_wait, avg_wait))
        # return [reward_avg, num_avg, avg_ratio, max_flow, avg_flow, max_wait, avg_wait]
        return dict(reward_avg=reward_avg,
                    num_avg=num_avg,
                    avg_ratio=avg_ratio,
                    max_flow=max_flow,
                    avg_flow=avg_flow,
                    max_wait=max_wait,
                    avg_wait=avg_wait
                    )

    def save_model(self, path):
        print('adding checkpoints...')
        checkpoint_path = path + 'model.pth.tar'
        torch.save(
                    {'actor': self.pi.state_dict(),
                     'critic': self.Q.state_dict(),
                     'target_critic': self.target_Q.state_dict(),
                     'actor_optimizer': self.pi_optimizer.state_dict(),
                     'critic_optimizer': self.Q_optimizer.state_dict()
                     },
                    checkpoint_path)

        return

    def load_model(self, path):
        print('networks loading...')
        checkpoint = torch.load(path + 'model.pth.tar', map_location=self.device)

        self.pi.load_state_dict(checkpoint['actor'])
        self.Q.load_state_dict(checkpoint['critic'])
        self.target_Q.load_state_dict(checkpoint['target_critic'])
        self.pi_optimizer.load_state_dict(checkpoint['actor_optimizer'])
        self.Q_optimizer.load_state_dict(checkpoint['critic_optimizer'])

        return

    def set_temperature(self, alpha):
        self.alpha = alpha
        return


def mask_mul(actions: List[int], num_actions: int) -> np.ndarray:
    # generate a multiplicative mask representing the set
    m = np.full(num_actions, 0.)
    # 1 if admissible, 0 else
    m[actions] = 1.
    return m


def mask_add(actions: List[int], num_actions: int) -> np.ndarray:
    # generate a additive mask representing the set
    m = np.full(num_actions, -np.inf)
    # 1 if admissible, 0 else
    m[actions] = 0.
    return m
