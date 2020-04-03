# -*- coding: utf-8 -*-
#
#
# This file is part of NEST.
#
# Copyright (C) 2004 The NEST Initiative
#
# NEST is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# NEST is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with NEST.  If not, see <http://www.gnu.org/licenses/>.


import matplotlib.pyplot as plt
# IMPORT LIBS
import pickle
import nest
import numpy as np
import os
import glob
import pandas as pd
from scipy.special import softmax

from . import growth_curves
from . import spike_generator, visualize
from l2l.optimizees.optimizee import Optimizee
from collections import OrderedDict, namedtuple

StructuralPlasticityOptimizeeParameters = namedtuple(
    'StructuralPlasticityOptimizeeParameters', ['seed', 'path'])


class StructuralPlasticityOptimizee(Optimizee):
    def __init__(self, traj, parameters):
        super().__init__(traj)
        seed = np.uint32(parameters.seed)
        self.random_state = np.random.RandomState(seed=seed)

        # SIMULATION PARAMETERS
        self.input_type = 'greyvalue'
        # simulated time (ms)
        self.t_sim = 1000.  # 60000.0
        self.warm_up_time = 4000.
        # simulation step (ms).
        self.dt = 0.1

        self.number_input_neurons = 784  # 80
        self.number_bulk_exc_neurons = 800
        self.number_bulk_inh_neurons = 400
        self.number_out_exc_neurons = 10
        self.number_out_inh_neurons = 10
        self.number_output_clusters = 10

        # Structural_plasticity properties
        self.update_interval = 0
        self.record_interval = 100.
        # rate of background Poisson input
        self.bg_rate = 1000.0
        self.neuron_model = 'iaf_psc_alpha'

        # SPECIFY NEURON PARAMETERS
        # self.model_params = {'tau_m': 10.0,  # membrane time constant (ms)
        #                     # excitatory synaptic time constant (ms)
        #                     'tau_syn_ex': 0.5,
        #                     # inhibitory synaptic time constant (ms)
        #                     'tau_syn_in': 0.5,
        #                     't_ref': 2.0,  # absolute refractory period (ms)
        #                     'E_L': -65.0,  # resting membrane potential (mV)
        #                     'V_th': -50.0,  # spike threshold (mV)
        #                     'C_m': 250.0,  # membrane capacitance (pF)
        #                     'V_reset': -65.0  # reset potential (mV)
        #                     }

        self.nodes_in = None
        self.nodes_bulk_e = None
        self.nodes_bulk_i = None
        self.nodes_out_e = None
        self.nodes_out_i = None
        self.input_spike_detector = None
        self.pixel_rate_generators = None
        self.noise = None

        self.mean_ca_e = []
        self.mean_ca_i = []
        self.total_connections_e = []
        self.total_connections_i = []

        self.mean_ca_e_out = [[] for _ in range(10)]
        self.mean_ca_i_out = [[] for _ in range(10)]
        self.total_connections_e_out_0 = []
        self.total_connections_i_out_0 = []

        self.rates = []
        self.net_structure_e = ()
        self.net_structure_i = ()

        self.psc_in = 585.0
        self.psc_e = 485.0
        self.psc_i = -485.0
        self.psc_c = 585.0
        self.psc_out = 100.0
        self.psc_ext = 6.2

        # synaptic dictionary with uniform weight distribution
        self.syn_dict_e = {"model": "random_synapse",
                           'weight': {"distribution": "normal",
                                      "mu": self.psc_e,
                                      "sigma": 100.}}
        self.syn_dict_i = {"model": "random_synapse_i",
                           'weight': {"distribution": "normal",
                                      "mu": self.psc_i,
                                      "sigma": 100.}}

        # ALL THE DIFFERENT GROWTH CURVES
        self.growth_curve_in_e = growth_curves.in_e

        self.growth_curve_bulk_e_e = growth_curves.bulk_e_e
        self.growth_curve_bulk_e_i = growth_curves.bulk_e_i
        self.growth_curve_bulk_i_e = growth_curves.bulk_i_e
        self.growth_curve_bulk_i_i = growth_curves.bulk_i_i

        self.growth_curve_out_e_e = growth_curves.out_e_e
        self.growth_curve_out_e_i = growth_curves.out_e_i
        self.growth_curve_out_i_e = growth_curves.out_i_e
        self.growth_curve_out_i_i = growth_curves.out_i_i

        self.path_e = parameters.path_e
        self.path_i = parameters.path_i
        self.ind_idx = traj.individual.ind_idx
        self.gen_idx = traj.individual.generation

        # TODO Remove following?
        self.target_px = None
        self.target_lbl = None
        self.other_px = None
        self.other_lbl = None
        self.test_px = None
        self.test_lbl = None
        

    def prepare_network(self):
        self.reset_kernel()
        self.create_nodes()
        self.create_synapses()
        self.create_input_spike_detectors()
        self.pixel_rate_generators = self.create_pixel_rate_generator(
            self.input_type)
        self.noise = nest.Create('poisson_generator')
        nest.PrintNetwork(depth=2)

    def reset_kernel(self):
        nest.ResetKernel()
        nest.set_verbosity('M_ERROR')
        nest.SetKernelStatus({'resolution': self.dt,
                              #'grng_seed': 0,
                              'local_num_threads': 4})

        #nest.SetStructuralPlasticityStatus({
        #    'structural_plasticity_update_interval': self.update_interval,
        #})

    def create_nodes(self):
        synaptic_elems_in = {
            'In_E_Axn': self.growth_curve_in_e,
        }
        synaptic_elems_bulk_e = {
            'Bulk_E_Den': self.growth_curve_bulk_e_e,
            'Bulk_I_Den': self.growth_curve_bulk_e_i,
            'Bulk_E_Axn': self.growth_curve_bulk_e_e,
        }
        synaptic_elems_bulk_i = {
            'Bulk_E_Den': self.growth_curve_bulk_i_e,
            'Bulk_I_Den': self.growth_curve_bulk_i_i,
            'Bulk_I_Axn': self.growth_curve_bulk_i_i,
        }

        self.nodes_in = nest.Create('iaf_psc_alpha',
                                    self.number_input_neurons,
                                    {'synaptic_elements': synaptic_elems_in})

        self.nodes_e = nest.Create('iaf_psc_alpha',
                                   self.number_bulk_exc_neurons,
                                   {
                                       'synaptic_elements': synaptic_elems_bulk_e})

        self.nodes_i = nest.Create('iaf_psc_alpha',
                                   self.number_bulk_inh_neurons,
                                   {
                                       'synaptic_elements': synaptic_elems_bulk_i})

        self.net_structure_e += self.nodes_in + self.nodes_e 
        self.net_structure_i += self.nodes_i

        self.nodes_out_e = []
        self.nodes_out_i = []

        for ii in range(self.number_output_clusters):
            synaptic_elems_out_e = {
                'Out_E_Den_{}'.format(ii): self.growth_curve_out_e_e[ii],
                'Out_I_Den_{}'.format(ii): self.growth_curve_out_e_i[ii],
                'Out_E_Axn_{}'.format(ii): self.growth_curve_out_e_e[ii],
            }
            self.nodes_out_e.append(nest.Create('iaf_psc_alpha',
                                                self.number_out_exc_neurons,
                                                {
                                                    'synaptic_elements': synaptic_elems_out_e}))

            synaptic_elems_out_i = {
                'Out_E_Den_{}'.format(ii): self.growth_curve_out_e_i[ii],
                'Out_I_Den_{}'.format(ii): self.growth_curve_out_i_i[ii],
                'Out_I_Axn_{}'.format(ii): self.growth_curve_out_i_i[ii],
            }
            self.nodes_out_i.append(nest.Create('iaf_psc_alpha',
                                                self.number_out_inh_neurons,
                                                {
                                                    'synaptic_elements': synaptic_elems_out_i}))

            self.net_structure_e += self.nodes_out_e[ii] 
            self.net_structure_i += self.nodes_out_i[ii]

    @staticmethod
    def create_synapses():
        nest.CopyModel('static_synapse', 'random_synapse')
        #nest.SetDefaults('random_synapse',
        #                 {'weight': 1.0,
        #                  'delay': 1.0})

        nest.CopyModel('static_synapse', 'random_synapse_i')
        #nest.SetDefaults('random_synapse_i',
        #                 {'weight': -1.,
        #                  'delay': 1.0})

    def create_input_spike_detectors(self):
        self.input_spike_detector = nest.Create("spike_detector",
                                                params={"withgid": True,
                                                        "withtime": True})

    def create_pixel_rate_generator(self, input_type):
        if input_type == 'greyvalue':
            return nest.Create("poisson_generator",
                               self.number_input_neurons)
        elif input_type == 'bellec':
            return nest.Create("spike_generator",
                               self.number_input_neurons)
        elif input_type == 'greyvalue_sequential':
            n_img = self.number_input_neurons
            rates, starts, ends = spike_generator.greyvalue_sequential(
                self.target_px[n_img], start_time=0, end_time=783, min_rate=0,
                max_rate=10)
            self.rates = rates
            # FIXME changed to len(rates) from len(offsets)
            self.pixel_rate_generators = nest.Create(
                "poisson_generator", len(rates))

    def connect_input_spike_detectors(self):
        nest.Connect(self.nodes_in, self.input_spike_detector)

    def connect_greyvalue_input(self):
        # Poisson to input neurons
        # syn_dict = {"model": "random_synapse"}
        nest.Connect(self.pixel_rate_generators, self.nodes_in, "one_to_one",
                     syn_spec=self.syn_dict_e)
        # Input neurons to bulk
        nest.Connect(self.nodes_in, self.nodes_e[0:len(self.nodes_in)],
                     "one_to_one", syn_spec=self.syn_dict_e)
        nest.Connect(self.nodes_in, self.nodes_i, syn_spec=self.syn_dict_i)

    def connect_greyvalue_sequential_input(self):
        # FIXME changed commented out
        # nest.SetStatus(pixel_rate_generators, generator_stats)
        # Poisson to input neurons
        nest.Connect(self.pixel_rate_generators, self.nodes_in, "one_to_one",
                     syn_spec=self.syn_dict_e)
        # Input neurons to bulk
        nest.Connect(self.nodes_in, self.nodes_e[0:len(self.nodes_in)],
                     "one_to_one", syn_spec=self.syn_dict_e)

    def connect_bellec_input(self):
        nest.Connect(self.pixel_rate_generators, self.nodes_in, "one_to_one")
        weights = {'distribution': 'uniform',
                   'low': self.psc_i, 'high': self.psc_e, }
        # 'mu': 0., 'sigma': 100.}
        syn_dict = {"model": "random_synapse", "weight": weights}
        conn_dict = {'rule': 'fixed_outdegree',
                     'outdegree': int(0.05 * self.number_bulk_exc_neurons)}
        nest.Connect(self.nodes_in, self.nodes_e,
                     conn_spec=conn_dict, syn_spec=syn_dict)
        conn_dict = {'rule': 'fixed_outdegree',
                     'outdegree': int(0.05 * self.number_bulk_inh_neurons)}
        nest.Connect(self.nodes_in, self.nodes_i,
                     conn_spec=conn_dict, syn_spec=syn_dict)

    # Set a very low rate to the input, for the case where no input is provided
    def clear_input(self):
        generator_stats = [{'rate': 1.0} for _ in
                           range(self.number_input_neurons)]
        nest.SetStatus(self.pixel_rate_generators, generator_stats)

    def set_growthrate_output(self, output_region, input_on, iteration):
        for ii in range(self.number_output_clusters):
            if input_on:
                if ii == output_region:
                    gre = growth_curves.correct_input_growth_curve_e
                    gri = growth_curves.correct_input_growth_curve_i
                else:
                    gre = growth_curves.other_input_growth_curve
                    gri = growth_curves.other_input_growth_curve
            else:
                gre = growth_curves.no_input_growth_curve
                gri = growth_curves.no_input_growth_curve
            if iteration > 10:
                gre['growth_rate'] = gre['growth_rate'] / (iteration % 10)
                gri['growth_rate'] = gre['growth_rate'] / (iteration % 10)

            synaptic_elems_out_e = {
                'Out_E_Den_{}'.format(ii): gre,
                'Out_I_Den_{}'.format(ii): gri,
                'Out_E_Axn_{}'.format(ii): gre,
            }
            nest.SetStatus(self.nodes_out_e[ii], 'synaptic_elements_param',
                           synaptic_elems_out_e)

            synaptic_elems_out_i = {
                'Out_E_Den_{}'.format(ii): gre,
                'Out_I_Den_{}'.format(ii): gri,
                'Out_I_Axn_{}'.format(ii): gri,
            }
            nest.SetStatus(self.nodes_out_i[ii], 'synaptic_elements_param',
                           synaptic_elems_out_i)

    # After a couple of iterations we want to freeze the bulk. We will do this
    # only by setting the growth rate to 0 in the dendritic synaptic elements
    # to still allow new connections to the output population.
    def freeze_bulk(self):
        freeze = {'growth_rate': 0.0}
        synaptic_elems_out_e = {
            'Bulk_E_Den': freeze,
            'Bulk_I_Den': freeze,
            # 'Bulk_E_Axn': freeze,
        }
        nest.SetStatus(self.nodes_e, 'synaptic_elements_param',
                       synaptic_elems_out_e)
        synaptic_elems_out_i = {
            'Bulk_E_Den': freeze,
            'Bulk_I_Den': freeze,
            # 'Bulk_I_Axn': freeze,
        }
        nest.SetStatus(self.nodes_i, 'synaptic_elements_param',
                       synaptic_elems_out_i)

    def connect_internal_bulk(self):
        # Connect bulk
        conn_dict = {'rule': 'fixed_outdegree',
                     'outdegree': int(0.09 * self.number_bulk_exc_neurons)}
        nest.Connect(self.nodes_e, self.nodes_e, conn_dict,
                     syn_spec=self.syn_dict_e)
        conn_dict = {'rule': 'fixed_outdegree',
                     'outdegree': int(0.1 * self.number_bulk_inh_neurons)}
        nest.Connect(self.nodes_e, self.nodes_i, conn_dict,
                     syn_spec=self.syn_dict_e)
        conn_dict = {'rule': 'fixed_outdegree',
                     'outdegree': int(0.12 * self.number_bulk_exc_neurons)}
        nest.Connect(self.nodes_i, self.nodes_e, conn_dict,
                     syn_spec=self.syn_dict_i)
        conn_dict = {'rule': 'fixed_outdegree',
                     'outdegree': int(0.08 * self.number_bulk_inh_neurons)}
        nest.Connect(self.nodes_i, self.nodes_i, conn_dict,
                     syn_spec=self.syn_dict_i)

    def connect_bulk_to_out(self):
        # Bulk to out
        conn_dict_e = {'rule': 'fixed_indegree',
                       'indegree': int(0.3 * self.number_bulk_exc_neurons)}
        conn_dict_i = {'rule': 'fixed_indegree',
                       'indegree': int(0.2 * self.number_bulk_exc_neurons)}
        for j in range(10):
            nest.Connect(self.nodes_e, self.nodes_out_e[j], conn_dict_e,
                         syn_spec=self.syn_dict_e)
            nest.Connect(self.nodes_e, self.nodes_out_i[j], conn_dict_i,
                         syn_spec=self.syn_dict_e)
            # TODO add connect i to e ?

    def connect_external_input(self, n_img):
        nest.SetStatus(self.noise, {"rate": self.bg_rate})
        nest.Connect(self.noise, self.nodes_e, 'all_to_all',
                     {'weight': self.psc_ext, 'delay': 1.0})
        nest.Connect(self.noise, self.nodes_i, 'all_to_all',
                     {'weight': self.psc_ext, 'delay': 1.0})

        if self.input_type == 'bellec':
            self.connect_bellec_input()
        elif self.input_type == 'greyvalue':
            self.connect_greyvalue_input()
        elif self.input_type == 'greyvalue_sequential':
            self.connect_greyvalue_sequential_input()

    def connect_internal_out(self):
        # Connect out
        conn_dict = {'rule': 'fixed_indegree', 'indegree': 2}
        syn_dict = {"model": "random_synapse"}
        conn_dict_i = {'rule': 'fixed_indegree', 'indegree': 2}
        syn_dict_i = {"model": "random_synapse"}
        for ii in range(10):
            nest.Connect(self.nodes_out_e[ii], self.nodes_out_e[ii], conn_dict,
                         syn_spec=syn_dict)
            nest.Connect(self.nodes_out_e[ii], self.nodes_out_i[ii], conn_dict,
                         syn_spec=syn_dict)
            nest.Connect(self.nodes_out_i[ii], self.nodes_out_e[ii],
                         conn_dict_i, syn_spec=syn_dict_i)
            nest.Connect(self.nodes_out_i[ii], self.nodes_out_i[ii],
                         conn_dict_i, syn_spec=syn_dict_i)

    def record_ca(self, record_mean=False):
        ca_e = nest.GetStatus(self.nodes_e, 'Ca'),  # Calcium concentration
        self.mean_ca_e.append(np.mean(ca_e))
        ca_i = nest.GetStatus(self.nodes_i, 'Ca'),  # Calcium concentration
        self.mean_ca_i.append(np.mean(ca_i))

        if record_mean:
            for ii in range(10):
                ca_e = nest.GetStatus(self.nodes_out_e[ii],
                                      'Ca'),  # Calcium concentration
                self.mean_ca_e_out[ii].append(np.mean(ca_e))
                ca_i = nest.GetStatus(self.nodes_out_i[ii],
                                      'Ca'),  # Calcium concentration
                self.mean_ca_i_out[ii].append(np.mean(ca_i))

    def clear_records(self):
        self.mean_ca_i_out.clear()
        self.mean_ca_e_out.clear()
        self.mean_ca_i.clear()
        self.mean_ca_e.clear()
        self.total_connections_e.clear()
        self.total_connections_i.clear()
        self.total_connections_e_out_0.clear()
        self.total_connections_i_out_0.clear()
        try:
            nest.SetStatus(self.input_spike_detector, {"n_events": 0})
        except AttributeError as e:
            print(e)
            pass

    def record_connectivity(self):
        syn_elems_e = nest.GetStatus(self.nodes_e, 'synaptic_elements')
        syn_elems_i = nest.GetStatus(self.nodes_i, 'synaptic_elements')
        self.total_connections_e.append(sum(neuron['Bulk_E_Axn']['z_connected']
                                            for neuron in syn_elems_e))
        self.total_connections_i.append(sum(neuron['Bulk_I_Axn']['z_connected']
                                            for neuron in syn_elems_i))
        # Visualize the connections from output 0. Hard coded for the moment
        syn_elems_e = nest.GetStatus(self.nodes_out_e[0], 'synaptic_elements')
        syn_elems_i = nest.GetStatus(self.nodes_out_i[0], 'synaptic_elements')
        self.total_connections_e_out_0.append(
            sum(neuron['Out_E_Axn_0']['z_connected']
                for neuron in syn_elems_e))
        self.total_connections_i_out_0.append(
            sum(neuron['Out_I_Axn_0']['z_connected']
                for neuron in syn_elems_i))

    def set_external_input(self, iteration, train_px_one, path='.'):
        random_id = np.random.randint(low=0, high=len(train_px_one))
        image = train_px_one[random_id]
        # Save image for reference
        plottable_image = np.reshape(image, (28, 28))
        plt.imshow(plottable_image, cmap='gray_r')
        plt.title('Index: {}'.format(random_id))
        save_path = os.path.join(path, 'normal_input{}.eps'.format(iteration))
        plt.savefig(save_path, format='eps')
        plt.close()
        if self.input_type == 'greyvalue':
            rates = spike_generator.greyvalue(image,
                                              min_rate=1, max_rate=100)
            generator_stats = [{'rate': w} for w in rates]
            nest.SetStatus(self.pixel_rate_generators, generator_stats)
        elif self.input_type == 'greyvalue_sequential':
            rates = spike_generator.greyvalue_sequential(image,
                                                         min_rate=1,
                                                         max_rate=100,
                                                         start_time=0,
                                                         end_time=783)
            generator_stats = [{'rate': w} for w in rates]
            nest.SetStatus(self.pixel_rate_generators, generator_stats)
        else:
            train_spikes, train_spike_times = spike_generator.bellec_spikes(
                train_px_one[random_id], self.number_input_neurons, self.dt)
            for ii, ii_spike_gen in enumerate(self.pixel_rate_generators):
                iter_neuron_spike_times = np.multiply(train_spikes[:, ii],
                                                      train_spike_times)
                nest.SetStatus([ii_spike_gen],
                               {"spike_times": iter_neuron_spike_times[
                                   iter_neuron_spike_times != 0],
                                "spike_weights": [1500.] * len(
                                    iter_neuron_spike_times[
                                        iter_neuron_spike_times != 0])}
                               )

    def plot_all(self, idx):
        spikes = nest.GetStatus(self.input_spike_detector, keys="events")[0]
        visualize.spike_plot(spikes, "Input spikes", idx=idx)
        visualize.plot_data(idx, self.mean_ca_e, self.mean_ca_i,
                            self.total_connections_e,
                            self.total_connections_i)
        visualize.plot_data_out(idx, self.mean_ca_e_out, self.mean_ca_i_out)
        visualize.plot_output(idx, self.mean_ca_e_out)

    def net_simulate(self):
        print("Starting simulation")
        nest.Simulate(self.warm_up_time)
        sim_steps = np.arange(0, self.t_sim, self.record_interval)
        for i, step in enumerate(sim_steps):
            nest.Simulate(self.record_interval)
            if i % 20 == 0:
                print("Progress: " + str(i / 2) + "%")
            self.record_ca()
            self.record_connectivity()
        print("Simulation loop finished successfully")

    def checkpoint(self, id):
        # Input connections
        connections = nest.GetStatus(nest.GetConnections(self.nodes_in))
        f = open('conn_input_{}.bin'.format(id), "wb")
        pickle.dump(connections, f, pickle.HIGHEST_PROTOCOL)
        f.close()

        # Bulk connections
        connections = nest.GetStatus(nest.GetConnections(self.nodes_e))
        f = open('conn_bulke_{}.bin'.format(id), "wb")
        pickle.dump(connections, f, pickle.HIGHEST_PROTOCOL)
        f.close()
        connections = nest.GetStatus(nest.GetConnections(self.nodes_i))
        f = open('conn_bulki_{}.bin'.format(id), "wb")
        pickle.dump(connections, f, pickle.HIGHEST_PROTOCOL)
        f.close()

        # Out connections
        connections = nest.GetStatus(nest.GetConnections(self.nodes_out_e[0]))
        f = open('conn_oute_0_{}.bin'.format(id), "wb")
        pickle.dump(connections, f, pickle.HIGHEST_PROTOCOL)
        f.close()
        connections = nest.GetStatus(nest.GetConnections(self.nodes_out_i[0]))
        f = open('conn_outi_0_{}.bin'.format(id), "wb")
        pickle.dump(connections, f, pickle.HIGHEST_PROTOCOL)
        f.close()

    def connect_network(self):
        """
        set up the network and return the weights
        """
        self.prepare_network()
        # Do the connections
        self.connect_internal_bulk()
        self.connect_external_input(self.number_input_neurons)
        self.connect_bulk_to_out()
        self.connect_input_spike_detectors()
        
        self.conns_e = nest.GetConnections(source=self.net_structure_e)
        save_connections(self.conns_e, self.gen_idx, indx, path=self.path_e)
        self.conns_i = nest.GetConnections(source=self.net_structure_i)
        save_connections(self.conns_i, self.gen_idx, indx, path=self.path_i)
        
    def create_individual(self, indx):
        self.connect_network()
        weights_e = np.random.normal(self.psc_e,100.,len(self.conns_e))
        weights_i = np.random.uniform(self.psc_i,100.,len(self.conns_i))
        return {'weights_e':conns_e, 'weights_i':conns_i}

    def simulate(self, traj):
        """
        Returns the value of the function chosen during initialization
        :param ~l2l.utils.trajectory.Trajectory traj: Trajectory
        :return: a single element :obj:`tuple` containing the value of the
            chosen function
        """
        # set lower simulation time
        # self.t_sim = 10000.
        # Start training/simulation
        self.prepare_network()
        self.gen_idx = traj.individual.generation
        self.ind_idx = traj.individual.ind_idx
        print('Iteration {}'.format(self.gen_idx))
        # self.prepare_connect_simulation()
        # load connections and set
        nest.PrintNetwork(depth=2)
        self.weights_e = traj.individual.weights_e
        self.weights_i = traj.individual.weights_i
        replace_weights(self.gen_idx, self.ind_idx, traj, self.path_e, weights_e)
        replace_weights(self.gen_idx, self.ind_idx, traj, self.path_i, weights_i)

        self._run_simulation(self.gen_idx,
                             traj.individual.train_px_one,
                             record_mean=True)

        model_out = softmax(
            [self.mean_ca_e_out[j][-1] for j in range(10)])
        # np.save('model_out.npy', model_out)
        # weights = enkf_run.run()
        # print(weights)
        # weights *= enkf_run.scaler
        # print('Scaler: ', enkf_run.scaler)
        # print(weights)
        self.plot_all(self.gen_idx)
        self.clear_records()
        # return connection weights
        # why do we return the connection weights? they don't change during the run
        conns = nest.GetConnections(source=self.net_structure_e)
        status_e = nest.GetStatus(conns)
        conns = nest.GetConnections(source=self.net_structure_i)
        status_i = nest.GetStatus(conns)
        we = [s.get('weight') for s in status_e]
        wi = [s.get('weight') for s in status_i]
        target_label = int(traj.individual.targets[0])
        target = np.zeros(10)
        target[target_label] = 1.0
        fitness = ((target - model_out) ** 2).sum()
        return dict(fitness=fitness, model_out=model_out,
                    weight_e=we, weight_i=wi)

    def _run_simulation(self, j, train_px_one, record_mean=False):
        """
        Convenience function may be removed later on
        """
        # Show a one
        self.set_external_input(j, train_px_one)
        # example.set_growthrate_output(0, True, i)
        self.net_simulate()
        # record ca
        # sim.clear_records()
        self.record_ca(record_mean=record_mean)
        print("One was shown")


def save_connections(conn, gen_idx, ind_idx, path='.'):
    status = nest.GetStatus(conn)
    d = OrderedDict({'source': [], 'target': [], 'weight': []})
    for elem in status:
        d['source'].append(elem.get('source'))
        d['target'].append(elem.get('target'))
        #d['weight'].append(elem.get('weight'))
    df = pd.DataFrame(d)
    df.to_pickle(
        os.path.join(path, 'connections_g{}_i{}.pkl'.format(gen_idx, ind_idx)))
    df.to_csv(
        os.path.join(path, 'connections_g{}_i{}.csv'.format(gen_idx, ind_idx)))


def replace_weights(gen_idx, ind_idx, traj, path='.', weights):
    if gen_idx == 0:
        conns = pd.read_csv(
            os.path.join(path, 'connections_g{}_i{}.csv'.format(gen_idx,
                                                                ind_idx)))
        #weights = conns['weight'].values
    else:
        conns = pd.read_csv(
            os.path.join(path, 'connections_g{}_i{}.csv'.format(0, 0)))
        #weights = traj.individual.connection_weights

    sources = conns['source'].values
    targets = conns['target'].values
    #weights = conns['weight'].values
    print('now replacing connection weights')
    for (s, t, w) in zip(sources, targets, weights):
        syn_spec = {'weight': w}
        nest.Connect(tuple([s]), tuple([t]), syn_spec=syn_spec,
                     conn_spec='one_to_one')
