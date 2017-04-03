import logging
from collections import namedtuple
import numpy as np
from ltl.optimizers.optimizer import Optimizer
from ltl import dict_to_list, list_to_dict
logger = logging.getLogger("ltl-ce")

ClonceParameters = namedtuple('CloneceParameters',
                                    ['pop_size', 'rho', 'smoothing', 'burn_in', 'distribution', 
                                     'parameterDistribution', 'stop_criterion', 'n_iteration'])
ClonceParameters.__new__.__defaults__ = (30, 0.2, 0, 1, None, None, 0.0, 30)

ClonceParameters.__doc__ = """
:param pop_size: Minimal number of individuals per simulation.
:param rho: Fraction of solutions to be considered elite in each iteration.

:param smoothing: This is a factor between 0 and 1 that determines the weight assigned to the previous distribution
  parameters while calculating the new distribution parameters. The smoothing is done as a linear combination of the 
  optimal parameters for the current data, and the previous distribution as follows:
    
    new_params = smoothing*old_params + (1-smoothing)*optimal_new_params

:param burn_in: This is the number how many burn in steps should be performed 
:param distribution: Distribution object to use. Has to implement a fit and sample function.
:param parameterDistribution: Distribution object to use for parameter distribution. Has to implement a fit and sample function.
:param stop_criterion: Stop if this fitness is reached.
:param n_iteration: This is the maximum amount of iterations to perform
"""


class ClonCEOptimizer(Optimizer):
    """
    Class for a generic CLONCE optimizer.
    In the pseudo code the algorithm does:

    For n iterations do:
      - Sample individuals from distribution
      - evaluate individuals and get fitness
      - pick rho * pop_size number of elite individuals
      - Fit the distribution family to the new elite individuals by minimizing cross entropy.
        The distribution fitting is smoothed to prevent premature convergence to local minima.
        A weight equal to the `smoothing` parameter is assigned to the previous parameters when
        smoothing.
      - Perform cloning step on the generated population
      - Perform Gibbs sampling for each parameter   

    return final distribution parameters.
    (The final distribution parameters contain information regarding the location of the maxima)
    
    NOTE: This expects all parameters of the system to be of numpy.float64. Note that this irritating
    restriction on the kind of floating point type rewired is put in place due to PyPet's crankiness
    regarding types.

    :param  ~pypet.trajectory.Trajectory traj:
      Use this pypet trajectory to store the parameters of the specific runs. The parameters should be
      initialized based on the values in `parameters`
    
    :param optimizee_create_individual:
      Function that creates a new individual. All parameters of the Individual-Dict returned should be
      of numpy.float64 type
    
    :param optimizee_fitness_weights: 
      Fitness weights. The fitness returned by the Optimizee is multiplied by these values (one for each
      element of the fitness vector)
    
    :param parameters: 
      Instance of :func:`~collections.namedtuple` :class:`CrossEntropyParameters` containing the
      parameters needed by the Optimizer
    
    :param optimizee_bounding_func:
      This is a function that takes an individual as argument and returns another individual that is
      within bounds (The bounds are defined by the function itself). If not provided, the individuals
      are not bounded.
    """

    def __init__(self, traj, optimizee_create_individual, optimizee_fitness_weights, parameters, 
                 optimizee_bounding_func=None):
        
        super().__init__(traj, optimizee_create_individual=optimizee_create_individual,
                         optimizee_fitness_weights=optimizee_fitness_weights, parameters=parameters)
        
        self.optimizee_bounding_func = optimizee_bounding_func

        if parameters.pop_size < 1:
            raise ValueError("pop_size needs to be greater than 0")
        if parameters.smoothing >= 1 or parameters.smoothing < 0:
            raise ValueError("smoothing has to be in interval [0, 1)")
        
        # The following parameters are recorded
        traj.f_add_parameter('pop_size', parameters.pop_size,
                                    comment='Number of minimal individuals simulated in each run')
        traj.f_add_parameter('rho', parameters.rho,
                                    comment='Fraction of individuals considered elite in each generation')
        traj.f_add_parameter('smoothing', parameters.smoothing,
                                    comment='Weight of old parameters in smoothing')
        traj.f_add_parameter('burn_in', parameters.burn_in,
                                    comment='Decay factor for temperature')
        traj.f_add_parameter('stop_criterion', parameters.stop_criterion,
                                    comment='Stop if best individual reaches this fitness')
        traj.f_add_parameter('n_iteration', parameters.n_iteration,
                                    comment='Number of iterations to run')       

        temp_indiv, self.optimizee_individual_dict_spec = dict_to_list(self.optimizee_create_individual(),
                                                                       get_dict_spec=True)
        traj.f_add_derived_parameter('dimension', len(temp_indiv),
                                     comment='The dimension of the parameter space of the optimizee')

        # Added a generation-wise parameter logging
        traj.results.f_add_result_group('generation_params',
                                        comment='This contains the optimizer parameters that are'
                                                ' common across a generation')
        
        # The following parameters are recorded as generation parameters i.e. once per generation
        self.g = 0  # the current generation
        # This is the value above which the samples are considered elite in the
        # current generation
        self.gamma = -np.inf
        self.pop_size = parameters.pop_size
        self.best_fitness_in_run = -np.inf

        # The first iteration does not pick the values out of the Gaussian distribution. It picks randomly
        # (or at-least as randomly as optimizee_create_individual creates individuals)
        
        # Note that this array stores individuals as an np.array of floats as opposed to Individual-Dicts
        # This is because this array is used within the context of the cross entropy algorithm and
        # Thus needs to handle the optimizee individuals as vectors
        current_eval_pop = [self.optimizee_create_individual() for _ in range(parameters.pop_size)]

        if optimizee_bounding_func is not None:
            current_eval_pop = [self.optimizee_bounding_func(ind) for ind in current_eval_pop]

        self.eval_pop = current_eval_pop
        self.eval_pop_asarray = np.array([dict_to_list(x) for x in self.eval_pop])
        
        # Max Likelihood
        self.current_distribution = parameters.distribution
        self.current_distribution.fit(self.eval_pop_asarray)
        
        self.parameterDistribution = parameters.parameterDistribution
        
        self._expand_trajectory(traj)

    def post_process(self, traj, fitnesses_results):
        """
        See :meth:`~ltl.optimizers.optimizer.Optimizer.post_process`
        """

        rho, pop_size, smoothing, burn_in, dimension, stop_criterion, n_iteration = \
            traj.rho, traj.pop_size, traj.smoothing, traj.burn_in, traj.dimension, traj.stop_criterion, traj.n_iteration
            
        weighted_fitness_list = []
        #**************************************************************************************************************
        # Storing run-information in the trajectory
        # Reading fitnesses and performing distribution update
        #**************************************************************************************************************
        for run_index, fitness in fitnesses_results:
            # We need to convert the current run index into an ind_idx
            # (index of individual within one generation)
            traj.v_idx = run_index
            ind_index = traj.par.ind_idx
            
            traj.f_add_result('$set.$.individual', self.eval_pop[ind_index])
            traj.f_add_result('$set.$.fitness', fitness)

            weighted_fitness_list.append(np.dot(fitness, self.optimizee_fitness_weights))
        traj.v_idx = -1  # set trajectory back to default

        # Performs descending arg-sort of weighted fitness
        fitness_sorting_indices = list(reversed(np.argsort(weighted_fitness_list)))

        # Sorting the data according to fitness
        sorted_population = self.eval_pop_asarray[fitness_sorting_indices]
        sorted_fitess = np.asarray(weighted_fitness_list)[fitness_sorting_indices]
        
        # Elite individuals are with performance better than or equal to the (1-rho) quantile.
        # See original describtion of cross entropy for optimization
        n_elite = int(rho * pop_size)
        elite_individuals = sorted_population[:n_elite]
        self.best_fitness_in_run = sorted_fitess[0]
        self.best_indv = sorted_population[0]
        self.gamma = sorted_fitess[n_elite - 1]
        
        #Check for stopping criterion
        if self.g > n_iteration or self.best_fitness_in_run > stop_criterion:
            return  

        logger.info("-- End of generation {} --".format(self.g))
        logger.info("  Evaluated %i individuals" % len(fitnesses_results))
        logger.info('  Best Fitness: {}'.format(self.best_fitness_in_run))
        logger.debug('  Calculated gamma: {}'.format(self.gamma))

        #**************************************************************************************************************
        # Storing Generation Parameters / Results in the trajectory
        #**************************************************************************************************************
        # These entries correspond to the generation that has been simulated prior to this post-processing run
        
        # Documentation of algorithm parameters for the current generation
        # 
        # generation          - The index of the evaluated generation
        # gamma               - The fitness threshold inferred from the evaluated  generation
        #                       (This is used in sampling the next generation)
        # best_fitness_in_run - The highest fitness among the individuals in the
        #                       evaluated generation
        # pop_size            - Population size
        generation_result_dict = {
            'generation': self.g,
            'gamma': self.gamma,
            'best_fitness_in_run': self.best_fitness_in_run,
            'pop_size': self.pop_size
        }

        generation_name = 'generation_{}'.format(self.g)
        traj.results.generation_params.f_add_result_group(generation_name)
        traj.results.generation_params.f_add_result(
            generation_name + '.algorithm_params', generation_result_dict,
            comment="These are the parameters that correspond to the algorithm, look at the source code"
                    " for `CrossEntropyOptimizer::post_process()` for comments documenting these"
                    " parameters")

        # new distribution fit
        individuals_to_be_fitted = elite_individuals
        
        # Fitting New distribution parameters.
        self.distribution_results = self.current_distribution.fit(individuals_to_be_fitted, smoothing)
        
        #Add the results of the distribution fitting to the trajectory
        for parameter_key, parameter_value in self.distribution_results.items():
            traj.results.generation_params.f_add_result(generation_name + '.' + parameter_key, parameter_value)
        
        #**************************************************************************************************************
        # Create the next generation by sampling the inferred distribution
        #**************************************************************************************************************
        # Note that this is only done in case the evaluated run is not the last run

        #Sample from the constructed distribution
        self.eval_pop_asarray = self.current_distribution.sample(self.pop_size)
        
        #Cloning
        #Perform the cloning step
        cloning_parameter = int(pop_size / (burn_in * n_elite) - 1)
        
        print('Used: ' + str(len(fitnesses_results)) + 'individuals with ' + str(cloning_parameter) + ' best: ' + str(self.best_fitness_in_run))
        
        fitnesses_results.clear()
        self.eval_pop.clear()
        
        #Generate the new cloned population
        cloned_population = self.eval_pop_asarray.copy()
        for i in range(len(self.eval_pop_asarray)):
            for j in range(cloning_parameter):
                cloned_population = np.concatenate((cloned_population, [self.eval_pop_asarray[i]]), axis=0)
        
#         np.random.shuffle(cloned_population)
        sampled_population = cloned_population.copy()
        
        #Apply Gibbs sampling by fitting a distribution over the parameters for the entire cloned population
        for i in range(len(cloned_population)):
            for j in range(burn_in):
                condSamples = cloned_population[:i]
                condSamples = np.concatenate((condSamples, sampled_population[i+1:]), axis=0)
                self.parameterDistribution.fit(condSamples)
#                 for j in range(burn_in):
                cloned_population[i] = self.parameterDistribution.sample(1)

        sampled_population = self.parameterDistribution.sample(len(sampled_population))
        
                
        self.eval_pop = [list_to_dict(ind_asarray, self.optimizee_individual_dict_spec)
                         for ind_asarray in cloned_population]
        self.eval_pop_asarray = np.array([dict_to_list(x) for x in self.eval_pop])
        self.g += 1  # Update generation counter
        self._expand_trajectory(traj)

    def end(self):
        """
        See :meth:`~ltl.optimizers.optimizer.Optimizer.end`
        """
        # ------------ Finished all runs and print result --------------- #
        logger.info("-- End of (successful) CLONCE optimization --")
        logger.info("-- Final distribution parameters --")
        for parameter_key, parameter_value in self.distribution_results.items():
            logger.info('  {}: {}'.format(parameter_key, parameter_value))
        logger.info("-- Best Individual --")
        logger.info(str(self.best_indv) + " with a fitness of: " + str(self.best_fitness_in_run))