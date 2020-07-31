#! /usr/bin/env python
# coding=utf-8
# Copyright (c) 2019 Uber Technologies, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
import copy
import functools
import itertools
import logging
import multiprocessing
import os
import signal
import subprocess as sp
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, List, Tuple

import numpy as np
from bayesmark.builtin_opt.pysot_optimizer import PySOTOptimizer
from bayesmark.space import JointSpace

from ludwig.constants import EXECUTOR, STRATEGY, MINIMIZE, COMBINED, LOSS, \
    VALIDATION, MAXIMIZE, TRAINING, TEST, CATEGORY, INT, REAL, TYPE, SPACE
from ludwig.data.postprocessing import postprocess
from ludwig.predict import predict, print_test_results, \
    save_prediction_outputs, save_test_statistics
from ludwig.train import full_train
from ludwig.utils.defaults import default_random_seed
from ludwig.utils.misc import get_class_attributes, get_from_registry, \
    set_default_value, set_default_values
from ludwig.utils.tf_utils import get_available_gpus

logger = logging.getLogger(__name__)


def int_grid_function(range: tuple, steps=None, **kwargs):
    low = range[0]
    high = range[1]
    if steps is None:
        steps = high - low + 1
    samples = np.linspace(low, high, num=steps, dtype=int)
    return samples.tolist()


def float_grid_function(range: tuple, steps=None, space='linear', base=None,
                        **kwargs):
    low = range[0]
    high = range[1]
    if steps is None:
        steps = int(high - low + 1)
    if space == 'linear':
        samples = np.linspace(low, high, num=steps)
    elif space == 'log':
        if base:
            samples = np.logspace(low, high, num=steps, base=base)
        else:
            samples = np.geomspace(low, high, num=steps)
    else:
        raise ValueError(
            'The space parameter of the float grid function is "{}". '
            'Available ones are: {"linear", "log"}'
        )
    return samples.tolist()


def category_grid_function(values, **kwargs):
    return values


grid_functions_registry = {
    'int': int_grid_function,
    'real': float_grid_function,
    'category': category_grid_function,
    'cat': category_grid_function
}


class HyperoptStrategy(ABC):
    def __init__(self, goal: str, parameters: Dict[str, Any]) -> None:
        assert goal in [MINIMIZE, MAXIMIZE]
        self.goal = goal  # useful for Bayesian strategy
        self.parameters = parameters

    @abstractmethod
    def sample(self) -> Dict[str, Any]:
        # Yields a set of parameters names and their values.
        # Define `build_hyperopt_strategy` which would take paramters as inputs
        pass

    def sample_batch(self, batch_size: int = 1) -> List[Dict[str, Any]]:
        samples = []
        for _ in range(batch_size):
            try:
                samples.append(self.sample())
            except IndexError:
                # Logic: is samples is empty it means that we encountered
                # the IndexError the first time we called self.sample()
                # so we should raise the exception. If samples is not empty
                # we should just return it, even if it will contain
                # less samples than the specified batch_size.
                # This is fine as from now on finished() will return True.
                if not samples:
                    raise IndexError
        return samples

    @abstractmethod
    def update(self, sampled_parameters: Dict[str, Any], metric_score: float):
        # Given the results of previous computation, it updates
        # the strategy (not needed for stateless strategies like "grid"
        # and random, but will be needed by Bayesian)
        pass

    def update_batch(self, parameters_metric_tuples: Iterable[
        Tuple[Dict[str, Any], float]]):
        for (sampled_parameters, metric_score) in parameters_metric_tuples:
            self.update(sampled_parameters, metric_score)

    @abstractmethod
    def finished(self) -> bool:
        # Should return true when all samples have been sampled
        pass


class RandomStrategy(HyperoptStrategy):
    num_samples = 10

    def __init__(self, goal: str, parameters: Dict[str, Any], num_samples=10,
                 **kwargs) -> None:
        HyperoptStrategy.__init__(self, goal, parameters)
        params_for_join_space = copy.deepcopy(parameters)
        for param_values in params_for_join_space.values():
            if param_values[TYPE] == CATEGORY:
                param_values[TYPE] = 'cat'
            if param_values[TYPE] == INT or param_values[TYPE] == REAL:
                if SPACE not in param_values:
                    param_values[SPACE] = 'linear'
        self.space = JointSpace(params_for_join_space)
        self.num_samples = num_samples
        self.samples = self._determine_samples()
        self.sampled_so_far = 0

    def _determine_samples(self):
        samples = []
        for _ in range(self.num_samples):
            bnds = self.space.get_bounds()
            x = bnds[:, 0] + (bnds[:, 1] - bnds[:, 0]) * np.random.rand(1, len(
                self.space.get_bounds()))
            sample = self.space.unwarp(x)[0]
            samples.append(sample)
        return samples

    def sample(self) -> Dict[str, Any]:
        if self.sampled_so_far >= len(self.samples):
            raise IndexError()
        sample = self.samples[self.sampled_so_far]
        self.sampled_so_far += 1
        return sample

    def update(self, sampled_parameters: Dict[str, Any], metric_score: float):
        pass

    def finished(self) -> bool:
        return self.sampled_so_far >= len(self.samples)


class GridStrategy(HyperoptStrategy):
    def __init__(self, goal: str, parameters: Dict[str, Any],
                 **kwargs) -> None:
        HyperoptStrategy.__init__(self, goal, parameters)
        self.search_space = self._create_search_space()
        self.samples = self._get_grids()
        self.sampled_so_far = 0

    def _create_search_space(self):
        search_space = {}
        for hp_name, hp_params in self.parameters.items():
            grid_function = get_from_registry(
                hp_params['type'], grid_functions_registry
            )
            search_space[hp_name] = grid_function(**hp_params)
        return search_space

    def _get_grids(self):
        hp_params = sorted(self.search_space)
        grids = [dict(zip(hp_params, prod)) for prod in itertools.product(
            *(self.search_space[hp_name] for hp_name in hp_params))]

        return grids

    def sample(self) -> Dict[str, Any]:
        if self.sampled_so_far >= len(self.samples):
            raise IndexError()
        sample = self.samples[self.sampled_so_far]
        self.sampled_so_far += 1
        return sample

    def update(
            self,
            sampled_parameters: Dict[str, Any],
            statistics: Dict[str, Any]
    ):
        # actual implementation ...
        pass

    def finished(self) -> bool:
        return self.sampled_so_far >= len(self.samples)


class PySOTStrategy(HyperoptStrategy):
    """pySOT: Surrogate optimization in Python.
    This is a wrapper around the pySOT package (https://github.com/dme65/pySOT):
        David Eriksson, David Bindel, Christine Shoemaker
        pySOT and POAP: An event-driven asynchronous framework for surrogate optimization
    """

    def __init__(self, goal: str, parameters: Dict[str, Any], num_samples=10,
                 **kwargs) -> None:
        HyperoptStrategy.__init__(self, goal, parameters)
        params_for_join_space = copy.deepcopy(parameters)
        for param_values in params_for_join_space.values():
            if param_values[TYPE] == CATEGORY:
                param_values[TYPE] = 'cat'
            if param_values[TYPE] == INT or param_values[TYPE] == REAL:
                if SPACE not in param_values:
                    param_values[SPACE] = 'linear'
        self.pysot_optimizer = PySOTOptimizer(params_for_join_space)
        self.sampled_so_far = 0
        self.num_samples = num_samples

    def sample(self) -> Dict[str, Any]:
        """Suggest one new point to be evaluated."""
        if self.sampled_so_far >= self.num_samples:
            raise IndexError()
        sample = self.pysot_optimizer.suggest(n_suggestions=1)[0]
        self.sampled_so_far += 1
        return sample

    def update(self, sampled_parameters: Dict[str, Any], metric_score: float):
        self.pysot_optimizer.observe([sampled_parameters], [metric_score])

    def finished(self) -> bool:
        return self.sampled_so_far >= self.num_samples


class HyperoptExecutor(ABC):
    def __init__(self, hyperopt_strategy: HyperoptStrategy,
                 output_feature: str, metric: str, split: str) -> None:
        self.hyperopt_strategy = hyperopt_strategy
        self.output_feature = output_feature
        self.metric = metric
        self.split = split

    def get_metric_score(self, eval_stats) -> float:
        return eval_stats[self.output_feature][self.metric]

    def sort_hyperopt_results(self, hyperopt_results):
        return sorted(
            hyperopt_results, key=lambda hp_res: hp_res["metric_score"],
            reverse=self.hyperopt_strategy.goal == MAXIMIZE
        )

    @abstractmethod
    def execute(
            self,
            model_definition,
            data_df=None,
            data_train_df=None,
            data_validation_df=None,
            data_test_df=None,
            data_csv=None,
            data_train_csv=None,
            data_validation_csv=None,
            data_test_csv=None,
            data_hdf5=None,
            data_train_hdf5=None,
            data_validation_hdf5=None,
            data_test_hdf5=None,
            train_set_metadata_json=None,
            experiment_name="hyperopt",
            model_name="run",
            model_load_path=None,
            model_resume_path=None,
            skip_save_training_description=False,
            skip_save_training_statistics=False,
            skip_save_model=False,
            skip_save_progress=False,
            skip_save_log=False,
            skip_save_processed_input=False,
            skip_save_unprocessed_output=False,
            skip_save_test_predictions=False,
            skip_save_test_statistics=False,
            output_directory="results",
            gpus=None,
            gpu_memory_limit=None,
            allow_parallel_threads=True,
            use_horovod=False,
            random_seed=default_random_seed,
            debug=False,
            **kwargs
    ):
        pass


class SerialExecutor(HyperoptExecutor):
    def __init__(
            self, hyperopt_strategy: HyperoptStrategy, output_feature: str,
            metric: str, split: str, **kwargs
    ) -> None:
        HyperoptExecutor.__init__(self, hyperopt_strategy, output_feature,
                                  metric, split)

    def execute(
            self,
            model_definition,
            data_df=None,
            data_train_df=None,
            data_validation_df=None,
            data_test_df=None,
            data_csv=None,
            data_train_csv=None,
            data_validation_csv=None,
            data_test_csv=None,
            data_hdf5=None,
            data_train_hdf5=None,
            data_validation_hdf5=None,
            data_test_hdf5=None,
            train_set_metadata_json=None,
            experiment_name="hyperopt",
            model_name="run",
            # model_load_path=None,
            # model_resume_path=None,
            skip_save_training_description=False,
            skip_save_training_statistics=False,
            skip_save_model=False,
            skip_save_progress=False,
            skip_save_log=False,
            skip_save_processed_input=False,
            skip_save_unprocessed_output=False,
            skip_save_test_predictions=False,
            skip_save_test_statistics=False,
            output_directory="results",
            gpus=None,
            gpu_memory_limit=None,
            allow_parallel_threads=True,
            use_horovod=False,
            random_seed=default_random_seed,
            debug=False,
            **kwargs
    ):
        hyperopt_results = []
        while not self.hyperopt_strategy.finished():
            sampled_parameters = self.hyperopt_strategy.sample_batch()
            metric_scores = []

            for parameters in sampled_parameters:
                modified_model_definition = substitute_parameters(
                    copy.deepcopy(model_definition), parameters)

                train_stats, eval_stats = train_and_eval_on_split(
                    modified_model_definition,
                    eval_split=self.split,
                    data_df=data_df,
                    data_train_df=data_train_df,
                    data_validation_df=data_validation_df,
                    data_test_df=data_test_df,
                    data_csv=data_csv,
                    data_train_csv=data_train_csv,
                    data_validation_csv=data_validation_csv,
                    data_test_csv=data_test_csv,
                    data_hdf5=data_hdf5,
                    data_train_hdf5=data_train_hdf5,
                    data_validation_hdf5=data_validation_hdf5,
                    data_test_hdf5=data_test_hdf5,
                    train_set_metadata_json=train_set_metadata_json,
                    experiment_name=experiment_name,
                    model_name=model_name,
                    # model_load_path=model_load_path,
                    # model_resume_path=model_resume_path,
                    skip_save_training_description=skip_save_training_description,
                    skip_save_training_statistics=skip_save_training_statistics,
                    skip_save_model=skip_save_model,
                    skip_save_progress=skip_save_progress,
                    skip_save_log=skip_save_log,
                    skip_save_processed_input=skip_save_processed_input,
                    skip_save_unprocessed_output=skip_save_unprocessed_output,
                    skip_save_test_predictions=skip_save_test_predictions,
                    skip_save_test_statistics=skip_save_test_statistics,
                    output_directory=output_directory,
                    gpus=gpus,
                    gpu_memory_limit=gpu_memory_limit,
                    allow_parallel_threads=allow_parallel_threads,
                    use_horovod=use_horovod,
                    random_seed=random_seed,
                    debug=debug,
                )
                metric_score = self.get_metric_score(eval_stats)
                metric_scores.append(metric_score)

                hyperopt_results.append(
                    {
                        "parameters": parameters,
                        "metric_score": metric_score,
                        "training_stats": train_stats,
                        "eval_stats": eval_stats,
                    }
                )

            self.hyperopt_strategy.update_batch(
                zip(sampled_parameters, metric_scores))

        hyperopt_results = self.sort_hyperopt_results(hyperopt_results)

        return hyperopt_results


class ParallelExecutor(HyperoptExecutor):
    num_workers = 2
    epsilon = 0.01
    epsilon_memory = 100
    TF_REQUIRED_MEMORY_PER_WORKER = 100

    def __init__(
            self,
            hyperopt_strategy: HyperoptStrategy,
            output_feature: str,
            metric: str,
            split: str,
            num_workers: int = 2,
            epsilon: float = 0.01,
            **kwargs
    ) -> None:
        HyperoptExecutor.__init__(self, hyperopt_strategy, output_feature,
                                  metric, split)
        self.num_workers = num_workers
        self.epsilon = epsilon
        self.queue = None

    @staticmethod
    def init_worker():
        signal.signal(signal.SIGINT, signal.SIG_IGN)

    def _train_and_eval_model(self, hyperopt_dict):
        parameters = hyperopt_dict["parameters"]
        train_stats, eval_stats = train_and_eval_on_split(**hyperopt_dict)
        metric_score = self.get_metric_score(eval_stats)

        return {
            "parameters": parameters,
            "metric_score": metric_score,
            "training_stats": train_stats,
            "eval_stats": eval_stats,
        }

    def _train_and_eval_model_gpu(self, hyperopt_dict):
        gpu_id_meta = self.queue.get()
        try:
            parameters = hyperopt_dict['parameters']
            hyperopt_dict["gpus"] = gpu_id_meta["gpu_id"]
            hyperopt_dict["gpu_memory_limit"] = gpu_id_meta["gpu_memory_limit"]
            train_stats, eval_stats = train_and_eval_on_split(**hyperopt_dict)
            metric_score = self.get_metric_score(eval_stats)
        finally:
            self.queue.put(gpu_id_meta)
        return {
            "parameters": parameters,
            "metric_score": metric_score,
            "training_stats": train_stats,
            "eval_stats": eval_stats,
        }

    def execute(
            self,
            model_definition,
            data_df=None,
            data_train_df=None,
            data_validation_df=None,
            data_test_df=None,
            data_csv=None,
            data_train_csv=None,
            data_validation_csv=None,
            data_test_csv=None,
            data_hdf5=None,
            data_train_hdf5=None,
            data_validation_hdf5=None,
            data_test_hdf5=None,
            train_set_metadata_json=None,
            experiment_name="hyperopt",
            model_name="run",
            # model_load_path=None,
            # model_resume_path=None,
            skip_save_training_description=False,
            skip_save_training_statistics=False,
            skip_save_model=False,
            skip_save_progress=False,
            skip_save_log=False,
            skip_save_processed_input=False,
            skip_save_unprocessed_output=False,
            skip_save_test_predictions=False,
            skip_save_test_statistics=False,
            output_directory="results",
            gpus=None,
            gpu_memory_limit=None,
            allow_parallel_threads=True,
            use_horovod=False,
            random_seed=default_random_seed,
            debug=False,
            **kwargs
    ):
        ctx = multiprocessing.get_context('spawn')

        hyperopt_parameters = []

        if gpus is None:
            available_gpus = get_available_gpus()
            if len(available_gpus) > 0:
                gpus = ','.join(available_gpus)

        if gpus is not None:

            num_available_cpus = ctx.cpu_count()

            if self.num_workers > num_available_cpus:
                logger.warning(
                    "WARNING: num_workers={}, num_available_cpus={}. "
                    "To avoid bottlenecks setting num workers to be less "
                    "or equal to number of available cpus is suggested".format(
                        self.num_workers, num_available_cpus
                    )
                )

            if isinstance(gpus, int):
                gpus = str(gpus)
            gpus = gpus.strip()
            gpu_ids = gpus.split(",")
            num_gpus = len(gpu_ids)

            available_gpu_memory_list = get_available_gpu_memory()
            gpu_ids_meta = {}

            if num_gpus < self.num_workers:
                fraction = (num_gpus / self.num_workers) - self.epsilon
                for gpu_id in gpu_ids:
                    available_gpu_memory = available_gpu_memory_list[
                        int(gpu_id)]
                    required_gpu_memory = fraction * available_gpu_memory

                    if gpu_memory_limit is None:
                        logger.warning(
                            'WARNING: Setting gpu_memory_limit to {} '
                            'as there available gpus are {} '
                            'and the num of workers is {} '
                            'and the available gpu memory for gpu_id '
                            '{} is {}'.format(
                                required_gpu_memory, num_gpus,
                                self.num_workers,
                                gpu_id, available_gpu_memory)
                        )
                        new_gpu_memory_limit = required_gpu_memory - \
                            (self.TF_REQUIRED_MEMORY_PER_WORKER * self.num_workers)
                    else:
                        new_gpu_memory_limit = gpu_memory_limit
                        if new_gpu_memory_limit > available_gpu_memory:
                            logger.warning(
                                'WARNING: Setting gpu_memory_limit to available gpu '
                                'memory {} minus an epsilon as the value specified is greater than '
                                'available gpu memory.'.format(
                                    available_gpu_memory)
                            )
                            new_gpu_memory_limit = available_gpu_memory - self.epsilon_memory

                        if required_gpu_memory < new_gpu_memory_limit:
                            if required_gpu_memory > 0.5 * available_gpu_memory:
                                if available_gpu_memory != new_gpu_memory_limit:
                                    logger.warning(
                                        'WARNING: Setting gpu_memory_limit to available gpu '
                                        'memory {} minus an epsilon as the gpus would be underutilized for '
                                        'the parallel processes otherwise'.format(
                                            available_gpu_memory)
                                    )
                                    new_gpu_memory_limit = available_gpu_memory - self.epsilon_memory
                            else:
                                logger.warning(
                                    'WARNING: Setting gpu_memory_limit to {} '
                                    'as the available gpus are {} and the num of workers '
                                    'are {} and the available gpu memory for gpu_id '
                                    '{} is {}'.format(
                                        required_gpu_memory, num_gpus,
                                        self.num_workers,
                                        gpu_id, available_gpu_memory)
                                )
                                new_gpu_memory_limit = required_gpu_memory
                        else:
                            logger.warning(
                                'WARNING: gpu_memory_limit could be increased to {} '
                                'as the available gpus are {} and the num of workers '
                                'are {} and the available gpu memory for gpu_id '
                                '{} is {}'.format(
                                    required_gpu_memory, num_gpus,
                                    self.num_workers,
                                    gpu_id, available_gpu_memory)
                            )

                    process_per_gpu = int(
                        available_gpu_memory / new_gpu_memory_limit)
                    gpu_ids_meta[gpu_id] = {
                        "gpu_memory_limit": new_gpu_memory_limit,
                        "process_per_gpu": process_per_gpu}
            else:
                for gpu_id in gpu_ids:
                    gpu_ids_meta[gpu_id] = {
                        "gpu_memory_limit": gpu_memory_limit,
                        "process_per_gpu": 1}

            manager = ctx.Manager()
            self.queue = manager.Queue()

            for gpu_id in gpu_ids:
                process_per_gpu = gpu_ids_meta[gpu_id]["process_per_gpu"]
                gpu_memory_limit = gpu_ids_meta[gpu_id]["gpu_memory_limit"]
                for _ in range(process_per_gpu):
                    gpu_id_meta = {"gpu_id": gpu_id,
                                   "gpu_memory_limit": gpu_memory_limit}
                    self.queue.put(gpu_id_meta)

        pool = ctx.Pool(self.num_workers,
                        ParallelExecutor.init_worker)
        hyperopt_results = []
        while not self.hyperopt_strategy.finished():
            sampled_parameters = self.hyperopt_strategy.sample_batch()

            for parameters in sampled_parameters:
                modified_model_definition = substitute_parameters(
                    copy.deepcopy(model_definition), parameters)

                hyperopt_parameters.append(
                    {
                        "parameters": parameters,
                        "model_definition": modified_model_definition,
                        "eval_split": self.split,
                        "data_df": data_df,
                        "data_train_df": data_train_df,
                        "data_validation_df": data_validation_df,
                        "data_test_df": data_test_df,
                        "data_csv": data_csv,
                        "data_train_csv": data_train_csv,
                        "data_validation_csv": data_validation_csv,
                        "data_test_csv": data_test_csv,
                        "data_hdf5": data_hdf5,
                        "data_train_hdf5": data_train_hdf5,
                        "data_validation_hdf5": data_validation_hdf5,
                        "data_test_hdf5": data_test_hdf5,
                        "train_set_metadata_json": train_set_metadata_json,
                        "experiment_name": experiment_name,
                        "model_name": model_name,
                        # model_load_path:model_load_path,
                        # model_resume_path:model_resume_path,
                        'skip_save_training_description': skip_save_training_description,
                        'skip_save_training_statistics': skip_save_training_statistics,
                        'skip_save_model': skip_save_model,
                        'skip_save_progress': skip_save_progress,
                        'skip_save_log': skip_save_log,
                        'skip_save_processed_input': skip_save_processed_input,
                        'skip_save_unprocessed_output': skip_save_unprocessed_output,
                        'skip_save_test_predictions': skip_save_test_predictions,
                        'skip_save_test_statistics': skip_save_test_statistics,
                        'output_directory': output_directory,
                        'gpus': gpus,
                        'gpu_memory_limit': gpu_memory_limit,
                        'allow_parallel_threads': allow_parallel_threads,
                        'use_horovod': use_horovod,
                        'random_seed': random_seed,
                        'debug': debug,
                    }
                )

            if gpus is not None:
                batch_results = pool.map(self._train_and_eval_model_gpu,
                                         hyperopt_parameters)
            else:
                batch_results = pool.map(self._train_and_eval_model,
                                         hyperopt_parameters)

            self.hyperopt_strategy.update_batch(
                (result["parameters"], result["metric_score"]) for result in
                batch_results
            )

            hyperopt_results.extend(batch_results)

        hyperopt_results = self.sort_hyperopt_results(hyperopt_results)
        return hyperopt_results


class FiberExecutor(HyperoptExecutor):
    num_workers = 2
    fiber_backend = "local"

    def __init__(
            self,
            hyperopt_strategy: HyperoptStrategy,
            output_feature: str,
            metric: str,
            split: str,
            num_workers: int = 2,
            num_cpus_per_worker: int = -1,
            num_gpus_per_worker: int = -1,
            fiber_backend: str = "local",
            **kwargs
    ) -> None:
        import fiber

        HyperoptExecutor.__init__(self, hyperopt_strategy, output_feature,
                                  metric, split)

        fiber.init(backend=fiber_backend)
        self.fiber_meta = fiber.meta

        self.num_cpus_per_worker = num_cpus_per_worker
        self.num_gpus_per_worker = num_gpus_per_worker

        self.resource_limits = {}
        if num_cpus_per_worker != -1:
            self.resource_limits["cpu"] = num_cpus_per_worker

        if num_gpus_per_worker != -1:
            self.resource_limits["gpu"] = num_gpus_per_worker

        self.num_workers = num_workers
        self.pool = fiber.Pool(num_workers)

    def execute(
            self,
            model_definition,
            data_df=None,
            data_train_df=None,
            data_validation_df=None,
            data_test_df=None,
            data_csv=None,
            data_train_csv=None,
            data_validation_csv=None,
            data_test_csv=None,
            data_hdf5=None,
            data_train_hdf5=None,
            data_validation_hdf5=None,
            data_test_hdf5=None,
            train_set_metadata_json=None,
            experiment_name="hyperopt",
            model_name="run",
            # model_load_path=None,
            # model_resume_path=None,
            skip_save_training_description=False,
            skip_save_training_statistics=False,
            skip_save_model=False,
            skip_save_progress=False,
            skip_save_log=False,
            skip_save_processed_input=False,
            skip_save_unprocessed_output=False,
            skip_save_test_predictions=False,
            skip_save_test_statistics=False,
            output_directory="results",
            gpus=None,
            gpu_memory_limit=None,
            allow_parallel_threads=True,
            use_horovod=False,
            random_seed=default_random_seed,
            debug=False,
            **kwargs
    ):
        train_func = functools.partial(
            train_and_eval_on_split,
            eval_split=self.split,
            data_df=data_df,
            data_train_df=data_train_df,
            data_validation_df=data_validation_df,
            data_test_df=data_test_df,
            data_csv=data_csv,
            data_train_csv=data_train_csv,
            data_validation_csv=data_validation_csv,
            data_test_csv=data_test_csv,
            data_hdf5=data_hdf5,
            data_train_hdf5=data_train_hdf5,
            data_validation_hdf5=data_validation_hdf5,
            data_test_hdf5=data_test_hdf5,
            train_set_metadata_json=train_set_metadata_json,
            experiment_name=experiment_name,
            model_name=model_name,
            # model_load_path=model_load_path,
            # model_resume_path=model_resume_path,
            skip_save_training_description=skip_save_training_description,
            skip_save_training_statistics=skip_save_training_statistics,
            skip_save_model=skip_save_model,
            skip_save_progress=skip_save_progress,
            skip_save_log=skip_save_log,
            skip_save_processed_input=skip_save_processed_input,
            skip_save_unprocessed_output=skip_save_unprocessed_output,
            skip_save_test_predictions=skip_save_test_predictions,
            skip_save_test_statistics=skip_save_test_statistics,
            output_directory=output_directory,
            gpus=gpus,
            gpu_memory_limit=gpu_memory_limit,
            allow_parallel_threads=allow_parallel_threads,
            use_horovod=use_horovod,
            random_seed=random_seed,
            debug=debug,
        )

        if self.resource_limits:
            train_func = self.fiber_meta(**self.resource_limits)(train_func)

        hyperopt_results = []
        while not self.hyperopt_strategy.finished():
            sampled_parameters = self.hyperopt_strategy.sample_batch()
            metric_scores = []

            stats_batch = self.pool.map(
                train_func,
                [
                    substitute_parameters(copy.deepcopy(model_definition),
                                          parameters)
                    for parameters in sampled_parameters
                ],
            )

            for stats, parameters in zip(stats_batch, sampled_parameters):
                train_stats, eval_stats = stats
                metric_score = self.get_metric_score(eval_stats)
                metric_scores.append(metric_score)

                hyperopt_results.append(
                    {
                        "parameters": parameters,
                        "metric_score": metric_score,
                        "training_stats": train_stats,
                        "eval_stats": eval_stats,
                    }
                )

            self.hyperopt_strategy.update_batch(
                zip(sampled_parameters, metric_scores))

        hyperopt_results = self.sort_hyperopt_results(hyperopt_results)

        return hyperopt_results


def get_build_hyperopt_strategy(strategy_type):
    return get_from_registry(strategy_type, strategy_registry)


def get_build_hyperopt_executor(executor_type):
    return get_from_registry(executor_type, executor_registry)


strategy_registry = {
    "grid": GridStrategy,
    "random": RandomStrategy,
    "pysot": PySOTStrategy,
}

executor_registry = {
    "serial": SerialExecutor,
    "parallel": ParallelExecutor,
    "fiber": FiberExecutor,
}


def update_hyperopt_params_with_defaults(hyperopt_params):
    set_default_value(hyperopt_params, STRATEGY, {})
    set_default_value(hyperopt_params, EXECUTOR, {})
    set_default_value(hyperopt_params, "split", VALIDATION)
    set_default_value(hyperopt_params, "output_feature", COMBINED)
    set_default_value(hyperopt_params, "metric", LOSS)
    set_default_value(hyperopt_params, "goal", MINIMIZE)

    set_default_values(hyperopt_params[STRATEGY], {"type": "random"})

    strategy = get_from_registry(hyperopt_params[STRATEGY]["type"],
                                 strategy_registry)
    strategy_defaults = {k: v for k, v in strategy.__dict__.items() if
                         k in get_class_attributes(strategy)}
    set_default_values(
        hyperopt_params[STRATEGY], strategy_defaults,
    )

    set_default_values(hyperopt_params[EXECUTOR], {"type": "serial"})

    executor = get_from_registry(hyperopt_params[EXECUTOR]["type"],
                                 executor_registry)
    executor_defaults = {k: v for k, v in executor.__dict__.items() if
                         k in get_class_attributes(executor)}
    set_default_values(
        hyperopt_params[EXECUTOR], executor_defaults,
    )


def set_values(model_dict, name, parameters_dict):
    if name in parameters_dict:
        params = parameters_dict[name]
        for key, value in params.items():
            if isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    model_dict[key][sub_key] = sub_value
            else:
                model_dict[key] = value


def get_parameters_dict(parameters):
    parameters_dict = {}
    for name, value in parameters.items():
        curr_dict = parameters_dict
        name_list = name.split(".")
        for i, name_elem in enumerate(name_list):
            if i == len(name_list) - 1:
                curr_dict[name_elem] = value
            else:
                name_dict = curr_dict.get(name_elem, {})
                curr_dict[name_elem] = name_dict
                curr_dict = name_dict
    return parameters_dict


def substitute_parameters(model_definition, parameters):
    parameters_dict = get_parameters_dict(parameters)
    for input_feature in model_definition["input_features"]:
        set_values(input_feature, input_feature["name"], parameters_dict)
    for output_feature in model_definition["output_features"]:
        set_values(output_feature, output_feature["name"], parameters_dict)
    set_values(model_definition["combiner"], "combiner", parameters_dict)
    set_values(model_definition["training"], "training", parameters_dict)
    set_values(model_definition["preprocessing"], "preprocessing",
               parameters_dict)
    return model_definition


def get_available_gpu_memory():
    _output_to_list = lambda x: x.decode('ascii').split('\n')[:-1]

    COMMAND = "nvidia-smi --query-gpu=memory.free --format=csv"
    try:
        memory_free_info = _output_to_list(sp.check_output(COMMAND.split()))[
                           1:]
        memory_free_values = [int(x.split()[0])
                              for i, x in enumerate(memory_free_info)]
    except Exception as e:
        print('"nvidia-smi" is probably not installed.', e)

    return memory_free_values


# TODo this is duplicate code from experiment,
#  reorganize experiment to avoid having to do this
def train_and_eval_on_split(
        model_definition,
        eval_split=VALIDATION,
        data_df=None,
        data_train_df=None,
        data_validation_df=None,
        data_test_df=None,
        data_csv=None,
        data_train_csv=None,
        data_validation_csv=None,
        data_test_csv=None,
        data_hdf5=None,
        data_train_hdf5=None,
        data_validation_hdf5=None,
        data_test_hdf5=None,
        train_set_metadata_json=None,
        experiment_name="hyperopt",
        model_name="run",
        # model_load_path=None,
        # model_resume_path=None,
        skip_save_training_description=False,
        skip_save_training_statistics=False,
        skip_save_model=False,
        skip_save_progress=False,
        skip_save_log=False,
        skip_save_processed_input=False,
        skip_save_unprocessed_output=False,
        skip_save_test_predictions=False,
        skip_save_test_statistics=False,
        output_directory="results",
        gpus=None,
        gpu_memory_limit=None,
        allow_parallel_threads=True,
        use_horovod=False,
        random_seed=default_random_seed,
        debug=False,
        **kwargs
):
    # Collect training and validation losses and metrics
    # & append it to `results`
    # ludwig_model = LudwigModel(modified_model_definition)
    (model, preprocessed_data, experiment_dir_name, train_stats,
     model_definition) = full_train(
        model_definition=model_definition,
        data_df=data_df,
        data_train_df=data_train_df,
        data_validation_df=data_validation_df,
        data_test_df=data_test_df,
        data_csv=data_csv,
        data_train_csv=data_train_csv,
        data_validation_csv=data_validation_csv,
        data_test_csv=data_test_csv,
        data_hdf5=data_hdf5,
        data_train_hdf5=data_train_hdf5,
        data_validation_hdf5=data_validation_hdf5,
        data_test_hdf5=data_test_hdf5,
        train_set_metadata_json=train_set_metadata_json,
        experiment_name=experiment_name,
        model_name=model_name,
        # model_load_path=model_load_path,
        # model_resume_path=model_resume_path,
        skip_save_training_description=skip_save_training_description,
        skip_save_training_statistics=skip_save_training_statistics,
        skip_save_model=skip_save_model,
        skip_save_progress=skip_save_progress,
        skip_save_log=skip_save_log,
        skip_save_processed_input=skip_save_processed_input,
        output_directory=output_directory,
        gpus=gpus,
        gpu_memory_limit=gpu_memory_limit,
        allow_parallel_threads=allow_parallel_threads,
        use_horovod=use_horovod,
        random_seed=random_seed,
        debug=debug,
    )
    (training_set, validation_set, test_set,
     train_set_metadata) = preprocessed_data
    if model_definition[TRAINING]["eval_batch_size"] > 0:
        batch_size = model_definition[TRAINING]["eval_batch_size"]
    else:
        batch_size = model_definition[TRAINING]["batch_size"]

    eval_set = validation_set
    if eval_split == TRAINING:
        eval_set = training_set
    elif eval_split == VALIDATION:
        eval_set = validation_set
    elif eval_split == TEST:
        eval_set = test_set

    test_results = predict(
        eval_set,
        train_set_metadata,
        model,
        model_definition,
        batch_size,
        evaluate_performance=True,
        debug=debug
    )
    if not (
            skip_save_unprocessed_output and skip_save_test_predictions and skip_save_test_statistics):
        if not os.path.exists(experiment_dir_name):
            os.makedirs(experiment_dir_name)

    # postprocess
    postprocessed_output = postprocess(
        test_results,
        model_definition["output_features"],
        train_set_metadata,
        experiment_dir_name,
        skip_save_unprocessed_output,
    )

    print_test_results(test_results)
    if not skip_save_test_predictions:
        save_prediction_outputs(postprocessed_output, experiment_dir_name)
    if not skip_save_test_statistics:
        save_test_statistics(test_results, experiment_dir_name)
    return train_stats, test_results