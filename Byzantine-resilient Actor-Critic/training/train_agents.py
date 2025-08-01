import datetime
import os
import numpy as np
import gym
from gym import spaces
import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import Input, Model, Sequential, layers
import pandas as pd

tf.get_logger().setLevel('ERROR')

'''
This file contains a function for training consensus AC agents in gym environments. It is designed for batch updates.
'''

def train_RPBCAC(env,agents,args,exp_buffer=None):
    '''
    FUNCTION train_RBPCAC() - training a mixed cooperative and adversarial network of consensus AC agents including RPBCAC agents
    The agents apply actions sampled from the actor network and estimate online the team-average errors for the critic and team-average reward updates.
    At the end of a sequence of episodes, the agents update the actor, critic, and team-average reward parameters in batches. All participating agents
    transmit their critic and team reward parameters but only cooperative agents perform resilient consensus updates. The critic and team-average reward
    networks are used for the evaluation of the actor gradient. In addition to the critic and team reward updates, the adversarial agents separately
    update their local critic that is used in their actor updates.

    ARGUMENTS: gym environment
               list of resilient consensus AC agents
               user-defined parameters for the simulation
    '''
    paths = []
    n_agents, n_states = env.n_agents, args['n_states']
    observation_dim = (n_agents + 1) * n_states
    n_coop = args['agent_label'].count('Cooperative')
    gamma = args['gamma']
    in_nodes = args['in_nodes']
    max_ep_len, n_episodes, n_ep_fixed = args['max_ep_len'], args['n_episodes'], args['n_ep_fixed']
    n_epochs, batch_size, buffer_size = args['n_epochs'], args['batch_size'], args['buffer_size']

    if exp_buffer:
        states = exp_buffer[0]
        nstates = exp_buffer[1]
        actions = exp_buffer[2]
        rewards = exp_buffer[3]
        observation = exp_buffer[4]
        nobservation = exp_buffer[5]
    else:
        states, nstates, actions, rewards, observations, nobservations = [], [], [], [], [], []
    #---------------------------------------------------------------------------
    '                                 TRAINING                                 '
    #---------------------------------------------------------------------------
    for t in range(n_episodes):
        j,  ep_returns = 0, 0
        est_returns, mean_true_returns, mean_true_returns_adv = [], 0, 0
        action, actor_loss, critic_loss, TR_loss = np.zeros(n_agents), np.zeros(n_agents), np.zeros(n_agents), np.zeros(n_agents)
        i = t % n_ep_fixed
        #-----------------------------------------------------------------------
        '                       BEGINNING OF EPISODE                           '
        #-----------------------------------------------------------------------
        env.reset()
        state, _ = env.get_data()
        observation = env.get_observations() 
        #-----------------------------------------------------------------------
        '       Evaluate expected returns at the beginning of episode           '
        #-----------------------------------------------------------------------
        for node in range(n_agents):
            if args['agent_label'][node] == 'Cooperative':
                current_obs = observation[:,node,:]
                obs_encoding = agents[node].encoder(current_obs)
                attention_out = agents[node].critic_attention_layer(observation, node)
                critic_in = tf.concat([obs_encoding, attention_out], axis=-1)
                est_returns.append(agents[node].critic(critic_in)[0][0].numpy())
        #-----------------------------------------------------------------------
        '                           Simulate episode                           '
        #-----------------------------------------------------------------------
        while j < max_ep_len:
            for node in range(n_agents):
                  action[node] = agents[node].get_action(observation)
            env.step(action)
            nstate, reward = env.get_data()
            nobservation = env.get_observations()
            ep_returns += reward * (gamma ** j)
            j += 1
            #-----------------------------------------------------------------------
            '                    Update experience replay buffers                  '
            #-----------------------------------------------------------------------
            states.append(np.array(state))
            nstates.append(np.array(nstate))
            actions.append(np.array(action).reshape(-1,1))
            rewards.append(np.array(reward).reshape(-1,1))
            observations.append(observation)  #tensor
            nobservations.append(nobservation)  #tensor
            state = np.array(nstate)
            observation = nobservation

            #------------------------------------------------------------------------
            '                             END OF EPISODE                            '
            #------------------------------------------------------------------------
            '                            ALGORITHM UPDATES                          '
            #------------------------------------------------------------------------
            if i == n_ep_fixed-1 and j == max_ep_len:
                # Convert experiences to tensors
                s = tf.convert_to_tensor(states,tf.float32)
                ns = tf.convert_to_tensor(nstates,tf.float32)
                r = tf.convert_to_tensor(rewards,tf.float32)
                a = tf.convert_to_tensor(actions,tf.float32)
                obs = tf.squeeze(observations,axis=1)
                nobs = tf.squeeze(nobservations,axis=1)
                sa = tf.concat([obs, a], axis=-1)  # [1, n_agents, observation_dim + 1]

                # Evaluate team-average reward of cooperative agents
                r_coop = tf.zeros([r.shape[0],r.shape[2]],tf.float32)
                for node in (x for x in range(n_agents) if args['agent_label'][x] == 'Cooperative'):
                    r_coop += r[:,node] / n_coop

                for n in range(n_epochs):
                    critic_weights,TR_weights = [],[]
                    #--------------------------------------------------------------------
                    '             I) LOCAL CRITIC AND TEAM-AVERAGE REWARD UPDATES       '
                    #--------------------------------------------------------------------
                    for node in range(n_agents):
                        r_applied = r_coop if args['common_reward'] else r[:,node]
                        if args['agent_label'][node] == 'Cooperative':
                            x, TR_loss[node] = agents[node].TR_update_local(sa,r_applied)
                            y, critic_loss[node] = agents[node].critic_update_local(obs,nobs,r_applied)
                        elif args['agent_label'][node] == 'Greedy':
                            x, TR_loss[node] = agents[node].TR_update_local(sa,r[:,node])
                            y, critic_loss[node] = agents[node].critic_update_local(obs,nobs,r[:,node])
                        elif args['agent_label'][node] == 'Malicious':
                            agents[node].critic_update_local(obs,nobs,r[:,node])
                            x, TR_loss[node] = agents[node].TR_update_compromised(sa,-r_coop)
                            y, critic_loss[node] = agents[node].critic_update_compromised(obs,nobs,-r_coop)
                        elif args['agent_label'][node] == 'Faulty':
                            x = agents[node].get_TR_weights()
                            y = agents[node].get_critic_weights()
                        TR_weights.append(x)
                        critic_weights.append(y)
                    #--------------------------------------------------------------------
                    '                     II) RESILIENT CONSENSUS UPDATES               '
                    #--------------------------------------------------------------------
                    for node in (x for x in range(n_agents) if args['agent_label'][x] == 'Cooperative'):
                        #----------------------------------------------------------------
                        '               a) RECEIVE PARAMETERS FROM NEIGHBORS            '
                        #----------------------------------------------------------------
                        critic_weights_innodes = [critic_weights[i] for i in in_nodes[node]]
                        TR_weights_innodes = [TR_weights[i] for i in in_nodes[node]]
                        #----------------------------------------------------------------
                        '               b) CONSENSUS UPDATES OF HIDDEN LAYERS           '
                        #----------------------------------------------------------------
                        agents[node].resilient_consensus_critic_hidden(critic_weights_innodes)
                        agents[node].resilient_consensus_TR_hidden(TR_weights_innodes)
                        #----------------------------------------------------------------
                        '               c) CONSENSUS OVER UPDATED ESTIMATES             '
                        #----------------------------------------------------------------
                        critic_agg = agents[node].resilient_consensus_critic(obs,critic_weights_innodes)
                        TR_agg = agents[node].resilient_consensus_TR(sa,TR_weights_innodes)
                        #----------------------------------------------------------------
                        '    d) STOCHASTIC UPDATES USING AGGREGATED ESTIMATION ERRORS   '
                        #----------------------------------------------------------------
                        agents[node].critic_update_team(obs,critic_agg)
                        agents[node].TR_update_team(sa,TR_agg)
                #--------------------------------------------------------------------
                '                           III) ACTOR UPDATES                      '
                #--------------------------------------------------------------------
                for node in range(n_agents):
                    if args['agent_label'][node] == 'Cooperative':
                        actor_loss[node] = agents[node].actor_update(obs[-max_ep_len*n_ep_fixed:],nobs[-max_ep_len*n_ep_fixed:],sa[-max_ep_len*n_ep_fixed:],a[-max_ep_len*n_ep_fixed:,node])
                    else:
                        actor_loss[node] = agents[node].actor_update(obs[-max_ep_len*n_ep_fixed:],nobs[-max_ep_len*n_ep_fixed:],r[-max_ep_len*n_ep_fixed:,node],a[-max_ep_len*n_ep_fixed:,node])
                #--------------------------------------------------------------------
                '                   IV) EXPERIENCE REPLAY BUFFER UPDATES             '
                #--------------------------------------------------------------------

                if len(states) > buffer_size:
                    q = len(states) - buffer_size
                    del states[:q]
                    del nstates[:q]
                    del actions[:q]
                    del rewards[:q]
                    del observations[:q]
                    del nobservations[:q]

        #----------------------------------------------------------------------------
        '                           TRAINING EPISODE SUMMARY                        '
        #----------------------------------------------------------------------------
        for node in range(n_agents):
            if args['agent_label'][node] == 'Cooperative':
                mean_true_returns += ep_returns[node]/n_coop
            else:
                mean_true_returns_adv += ep_returns[node]/(n_agents-n_coop)
        if t==0 and i==0:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            log_filename = f'log_{timestamp}.txt'
            run_dir = f"run_{timestamp}"
            # Create a directory with the timestamp
            os.makedirs(run_dir, exist_ok=True)
        output = '| Episode: {} | Est. returns: {} | Returns: {} | Average critic loss: {} | Average TR loss: {} | Average actor loss: {}'.format(t,est_returns,mean_true_returns,critic_loss,TR_loss,actor_loss)
        print(output)
        with open(log_filename, 'a') as file:
            file.write(output + '\n')        
        path = {
                "True_team_returns":mean_true_returns,
                "True_adv_returns":mean_true_returns_adv,
                "Estimated_team_returns":np.mean(est_returns)
               }
        paths.append(path)


    sim_data = pd.DataFrame.from_dict(paths)

    sim_data.to_pickle(f"./{run_dir}/sim_data_end.pkl")
    weights = [agent.get_parameters() for agent in agents]
    return weights,sim_data
