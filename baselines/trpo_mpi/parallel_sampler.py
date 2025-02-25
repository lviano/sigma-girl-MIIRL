from multiprocessing import Process, Queue, Event
import os
import baselines.common.tf_util as U
import time
import sys
from mpi4py import MPI
from baselines.common import set_global_seeds as set_all_seeds
import numpy as np
import tensorflow as tf

def traj_segment_function(pi, env, n_episodes,horizon, timesteps, stochastic):
    '''
    Collects trajectories
    '''

    # Initialize state variables
    t = 0
    ac = env.action_space.sample()
    new = True
    rew=0.0
    ob = env.reset()

    cur_ep_ret = 0
    cur_ep_len = 0
    ep_rets = []
    ep_lens = []

    # Initialize history arrays
    obs = np.array([ob for _ in range(horizon * n_episodes)])
    rews = np.zeros(horizon * n_episodes, 'float32')
    vpreds = np.zeros(horizon * n_episodes, 'float32')
    news = np.zeros(horizon * n_episodes, 'int32')
    acs = np.array([ac for _ in range(horizon * n_episodes)])
    prevacs = acs.copy()
    #mask = np.ones(horizon * n_episodes, 'float32')

    i = 0
    j = 0
    while True:
        prevac = ac
        ac, vpred,_,_ = pi.step(ob,stochastic=stochastic)
        # Slight weirdness here because we need value function at time T
        # before returning segment [0, T-1] so we get the correct
        # terminal value
        #if t > 0 and t % horizon == 0:
        if t == timesteps:
            return {"ob" : obs, "rew" : rews, "vpred" : vpreds, "new" : news,
                    "ac" : acs, "prevac" : prevacs, "nextvpred": vpred * (1 - new),
                    "ep_rets" : ep_rets, "ep_lens" : ep_lens,}

        obs[t] = ob
        vpreds[t] = vpred
        news[t] = new
        acs[t] = ac
        prevacs[t] = prevac

        ob, rew, new, _ = env.step(ac)
        #print("Step %s" %(j))
        rews[t] = rew

        cur_ep_ret += rew
        cur_ep_len += 1
        j += 1
        if new or j == horizon:
            new = True
            env.done = True

            ep_rets.append(cur_ep_ret)
            ep_lens.append(cur_ep_len)

            cur_ep_ret = 0
            cur_ep_len = 0
            ob = env.reset()
            i += 1
            j = 0
        t += 1


class Worker(Process):
    '''
    A worker is an independent process with its own environment and policy instantiated locally
    after being created. It ***must*** be runned before creating any tensorflow session!
    '''

    def __init__(self, output, input, event, make_env, make_pi, traj_segment_generator, seed,index):
        super(Worker, self).__init__()
        self.output = output
        self.input = input
        self.make_env = make_env
        self.make_pi = make_pi
        self.traj_segment_generator = traj_segment_generator
        self.event = event
        self.seed = seed
        self.index=index

    def close_env(self):
        self.env.close()

    def run(self):

        sess = U.single_threaded_session()
        sess.__enter__()

        env = self.make_env(self.index)
        self.env=env
        env.reset()
        workerseed = self.seed + 10000 * MPI.COMM_WORLD.Get_rank()
        set_all_seeds(workerseed)
        env.seed(workerseed)
        scope = 'pi%s' % os.getpid()
        pi = self.make_pi(scope, env)
        var = get_pi_trainable_variables(scope)
        set_from_flat = U.SetFromFlat(var)

        print('Worker %s - Running with seed %s' % (os.getpid(), workerseed))

        while True:
            self.event.wait()
            self.event.clear()
            command, weights = self.input.get()
            if command == 'collect':
                set_from_flat(weights)
                #print('Worker %s - Collecting...' % os.getpid())
                samples = self.traj_segment_generator(pi, env)
                self.output.put((os.getpid(), samples))
            elif command == 'exit':
                print('Worker %s - Exiting...' % os.getpid())
                env.close()
                sess.close()
                break

class ParallelSampler(object):

    def __init__(self, make_pi, make_env,n_workers, stochastic,horizon,timesteps,seed=0):
        self.n_workers=n_workers

        print('Using %s CPUs' % self.n_workers)

        if seed is None:
            seed = time.time()

        self.output_queue = Queue()
        self.input_queues = [Queue() for _ in range(self.n_workers)]
        self.events = [Event() for _ in range(self.n_workers)]

        n_episodes=n_workers
        n_episodes_per_process = n_episodes // self.n_workers
        print("%s episodes per worker" %n_episodes_per_process)
        remainder = n_episodes % self.n_workers

        f = lambda pi, env: traj_segment_function(pi, env, n_episodes_per_process,horizon, horizon,  stochastic)
        #f_rem = lambda pi, env: traj_segment_function(pi, env, n_episodes_per_process+1, horizon, stochastic)
        fun = [f] * (self.n_workers) #+ [f_rem] * remainder
        self.workers = [Worker(self.output_queue, self.input_queues[i], self.events[i], make_env, make_pi, fun[i], seed + i,i) for i in range(self.n_workers)]

        for w in self.workers:
            w.start()


    def collect(self, actor_weights):
        for i in range(self.n_workers):
            self.input_queues[i].put(('collect', actor_weights))

        for e in self.events:
            e.set()

        sample_batches = []
        for i in range(self.n_workers):
            pid, samples = self.output_queue.get()
            sample_batches.append(samples)

        return self._merge_sample_batches(sample_batches)

    def _merge_sample_batches(self, sample_batches):
        '''
        {"ob": obs, "rew": rews, "vpred": vpreds, "new": news,
         "ac": acs, "prevac": prevacs, "nextvpred": vpred * (1 - new),
         "ep_rets": ep_rets, "ep_lens": ep_lens}
         '''
        np_fields = ['ob', 'rew', 'vpred', 'new', 'ac', 'prevac']
        list_fields = ['ep_rets', 'ep_lens']

        new_dict = list(zip(np_fields, map(lambda f: sample_batches[0][f], np_fields))) + \
                   list(zip(list_fields,map(lambda f: sample_batches[0][f], list_fields))) + \
                   [('nextvpred', sample_batches[-1]['nextvpred'])]
        new_dict = dict(new_dict)

        for batch in sample_batches[1:]:
            for f in np_fields:
                new_dict[f] = np.concatenate((new_dict[f], batch[f]))
            for f in list_fields:
                new_dict[f].extend(batch[f])

        return new_dict


    def close(self):
        for i in range(self.n_workers):
            self.input_queues[i].put(('exit', None))

        for e in self.events:
            e.set()

        for w in self.workers:
            #w.close_env()
            w.join()


def get_trainable_variables(scope):
    return tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope)
def get_pi_trainable_variables(scope):
    return [v for v in get_trainable_variables(scope) if 'pi' in v.name[len(scope):].split('/')]