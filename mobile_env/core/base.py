import string
from collections import Counter, defaultdict
from typing import Dict, List, Set, Tuple, Optional

import gymnasium
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import math
import numpy as np
import pandas as pd
import pygame
from matplotlib import cm
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
from pygame import Surface

from mobile_env.core import metrics
from mobile_env.core.arrival import NoDeparture
from mobile_env.core.channels import OkumuraHata
from mobile_env.core.entities import BaseStation, UserEquipment, Sensor
from mobile_env.core.monitoring import Monitor
from mobile_env.core.movement import RandomWaypointMovement
from mobile_env.core.schedules import ResourceFair, RoundRobin
from mobile_env.core.util import BS_SYMBOL, SENSOR_SYMBOL, deep_dict_merge
from mobile_env.core.utilities import BoundedLogUtility
from mobile_env.core.job_manager import JobGenerationManager
from mobile_env.core.job_transfer import JobTransferManager
from mobile_env.core.job_process import JobProcessManager
from mobile_env.core.job_dataframe import JobDataFrame
from mobile_env.core.logger import LoggerManager

from mobile_env.handlers.delay import DelayManager
from mobile_env.handlers.smart_city_handler import MComSmartCityHandler

class MComCore(gymnasium.Env):
    NOOP_ACTION = 0
    metadata = {"render_modes": ["rgb_array", "human"]}

    def __init__(self, stations, users, sensors, config={}, render_mode=None):
        super().__init__()

        self.render_mode = render_mode
        assert render_mode in self.metadata["render_modes"] + [None]

        # set unspecified parameters to default configuration
        config = deep_dict_merge(self.default_config(), config)
        config = self.seeding(config)

        self.width, self.height = config["width"], config["height"]
        self.seed = config["seed"]
        self.reset_rng_episode = config["reset_rng_episode"]

        # set up arrival pattern, channel, movement, etc.
        self.arrival = config["arrival"](**config["arrival_params"])
        self.channel = config["channel"](**config["channel_params"])
        self.scheduler = config["scheduler"](**config["scheduler_params"])
        self.movement = config["movement"](**config["movement_params"])
        self.utility = config["utility"](**config["utility_params"])

        # define parameters that track the simulation's progress
        self.EP_MAX_TIME = config["EP_MAX_TIME"]
        self.time = None
        self.closed = False

        # defines the simulation's overall basestations and UEs
        self.stations = {bs.bs_id: bs for bs in stations}
        self.users = {ue.ue_id: ue for ue in users}
        self.sensors = {sensor.sensor_id: sensor for sensor in sensors}
        self.NUM_STATIONS = len(self.stations)
        self.NUM_USERS = len(self.users)
        self.NUM_SENSORS = len(self.sensors)

        # define sizes of base feature set that can or cannot be observed
        self.feature_sizes = {
            "queue_lengths": 4
        }

        # set object that handles calls to action(), reward() & observation()
        # set action & observation space according to handler
        self.handler = config["handler"]
        self.action_space = self.handler.action_space(self)
        self.observation_space = self.handler.observation_space(self)

        # stores what UEs are currently active, i.e., request service
        self.active: List[UserEquipment] = None
        # stores what sensors are currently active, i.e., request service
        self.active_sensor: List[Sensor] = None
        # stores what downlink connections between BSs and UEs are active
        self.connections: Dict[BaseStation, Set[UserEquipment]] = None
        # stores what downlink connections between BSs and Sensors are active
        self.connections_sensor: Dict[BaseStation, Set[Sensor]] = None
        # stores datarate of downlink connections between UEs and BSs
        self.datarates: Dict[Tuple[BaseStation, UserEquipment], float] = None
        # stores datarate of downlink connections between UEs and BSs
        self.datarates_sensor: Dict[Tuple[BaseStation, Sensor], float] = None
        # store resource allocations for each base station
        self.resource_allocations: Dict[str, List[float]] = None
        # store aori for each device
        self.aori_per_device: Dict[UserEquipment, float] = None
        # store aosi for each device
        self.aosi_per_device: Dict[Sensor, float] = None
        # store traffic requests for each device
        self.traffic_requests_per_device: Dict[UserEquipment, float] = None
        # store traffic requests for each sensor
        self.traffic_requests_per_sensor: Dict[Sensor, float] = None
        # store computation requests for each device
        self.computation_requests_per_device: Dict[UserEquipment, float] = None
        # store computation requests for each sensor
        self.computation_requests_per_sensor: Dict[Sensor, float] = None
        # stores each UE's (scaled) utility
        self.utilities: Dict[UserEquipment, float] = None
        # stores each Sensor's (scaled) utility
        self.utilities_sensor: Dict[Sensor, float] = None
        # define RNG (as of now: unused)
        self.rng = None

        # Instantiate the logger
        self.logger = LoggerManager(self)

        # Instratntiate the elay manager
        self.delay_manager = DelayManager(self)

        # Instantiate the data frames for jobs
        self.job_dataframe = JobDataFrame(self)

        # Instantiate JobGenerator, JobTransferManager and JobProcessManager classes
        self.job_generator = JobGenerationManager(self)
        self.job_transfer_manager = JobTransferManager(self)
        self.job_process_manager = JobProcessManager(self, self.job_dataframe)

        # Inititalize rewards
        self.timestep_reward = 0
        self.episode_reward = 0

        # parameters for pygame visualization
        self.window = None
        self.clock = None
        self.conn_isolines = None
        self.mb_isolines = None

        # add metrics required for visualization & set up monitor
        config["metrics"]["scalar_metrics"].update(
            {
                "number UE connections": metrics.number_connections,
                "number sensor conncections": metrics.number_connections_sensor,
                "delayed UE packet count": metrics.delayed_ue_packets,
                "delayed sensor packet count": metrics.delayed_sensor_packets,
                "mean utility": metrics.mean_utility,
                "mean utility sensor": metrics.mean_utility_sensor,
                "mean datarate": metrics.mean_datarate,
                "mean datarate sensor": metrics.mean_datarate_sensor,
            }
        )

        config["metrics"]["performance_metrics"].update(
            {   
                "bw allocation UE": metrics.bandwidth_allocation_ue,
                "bw allocation sensor": metrics.bandwidth_allocation_sensor,
                "comp. allocation UE": metrics.computational_allocation_ue,
                "comp. allocation sensor": metrics.computational_allocation_sensor,    
                "total delayed packets": metrics.delayed_ue_packets,         
                "reward": metrics.get_reward,
                "reward cumulative": metrics.get_episode_reward,   
                "total aori":metrics.calculate_total_aori,
                "total aosi": metrics.calculate_total_aosi,
            },
        )

        config["metrics"]["system_metrics"].update(
            { 
                "total traffic request ue": metrics.get_total_traffic_request_ue,
                "total traffic request sensor": metrics.get_total_traffic_request_sensor,
                "total computation request ue": metrics.get_total_computation_request_ue,
                "total computation request sensor": metrics.get_total_computation_request_sensor,
                "total throughput ue": metrics.calculate_total_throughput_ue,
                "total throughput sensor": metrics.calculate_total_throughput_sensor,
            },
        )

        config["metrics"]["bs_metrics"].update(
            {   
                "queue size transferred UE jobs": metrics.get_bs_transferred_ue_queue_size,
                "queue size transferred sensor jobs": metrics.get_bs_transferred_sensor_queue_size,
                "queue size accomplished UE jobs": metrics.get_bs_accomplished_ue_queue_size,
                "queue size accomplished sensor jobs": metrics.get_bs_accomplished_sensor_queue_size,
            },
        )
        
        config["metrics"]["ue_metrics"].update(
            {
                "distance UE-station": metrics.user_closest_distance, 
                "user queue size": metrics.get_ue_data_queues,
                "traffic request": metrics.get_traffic_request_ue,
                "computation request": metrics.get_computation_request_ue,
                "user throughput": metrics.calculate_throughput_ue,
                "user datarate": metrics.get_datarate_ue,
                "user utility": metrics.user_utility, 
                "AoRI": metrics.get_aori,
                "AoSI": metrics.get_aosi,
            },
        )

        config["metrics"]["ss_metrics"].update(
            {
                "distance sensor-station": metrics.sensor_closest_distance, 
                "sensor queue size": metrics.get_sensor_data_queues,
                "traffic request": metrics.get_traffic_request_sensor,
                "computation request": metrics.get_computation_request_sensor,
                "sensor throughput": metrics.calculate_throughput_sensor,
                "sensor datarate": metrics.get_datarate_sensor,
                "sensor utility": metrics.user_utility_sensor, 
            }
        )

        self.monitor = Monitor(**config["metrics"])

    @classmethod
    def default_config(cls):
        """Set default configuration of environment dynamics."""
        # set up configuration of environment
        width, height = 200, 200
        ep_time = 100
        config = {
            # environment parameters:
            "width": width,
            "height": height,
            "EP_MAX_TIME": ep_time,
            "seed": 666,
            "reset_rng_episode": False,
            # used simulation models:
            "arrival": NoDeparture,
            "channel": OkumuraHata,
            "scheduler": ResourceFair,
            "movement": RandomWaypointMovement,
            "utility": BoundedLogUtility,
            "handler": MComSmartCityHandler,
            # default cell config
            "bs": {
                "bw": 100e6,                    # in Hz (100 MHz)
                "freq": 3500,                   # in MHz
                "tx": 40,                       # in dBm
                "height": 40,                   # in m
                "computational_power": 100,     # in units
            },
            # default UE config
            "ue": {
                "velocity": 1.5,    # Typical urban smart city device speed (10 km/h)
                "snr_tr": 2e-8,     # SNR threshold (-3 dB for good connection)
                "noise": 1e-9,      # noise power in Watts
                "height": 1.5,      # in m
            },
            # default Sensor config
            "sensor": {
                "height": 1.5,
                "snr_tr": 2e-8,         
                "noise": 1e-9,
            },
            # default ue job generation config
            "ue_job": {
                "job_generation_probability": 0.7,
                "communication_job_lambda_value": 100.0,     # in Mbps
                "computation_job_lambda_value": 10.0,       # in units
            },
            # default sensor job generation config
            "sensor_job": {
                "communication_job_lambda_value": 40.0,      # in Mbps
                "computation_job_lambda_value": 4.0,         # in units
            },
            # default delay threshold for packets
            "e2e_delay_threshold": 2.0,
            "reward_calculation": {
                "ue_penalty": -5.0,
                "sensor_penalty": -2.0,
                "base_reward": 10.0,
                "synch_base_reward": 10.0,
                "discount_factor": 0.95,
                "positive_discount_factor": 0.9,      # Discount factor for positive delay
                "negative_discount_factor": 0.8,      # Discount factor for negative delay
            }
        }

        # set up default configuration parameters for arrival pattern, ...
        aparams = {"ep_time": ep_time, "reset_rng_episode": False}
        config.update({"arrival_params": aparams})
        config.update({"channel_params": {}})
        config.update({"scheduler_params": {"quantum": 7e6}})
        mparams = {
            "width": width,
            "height": height,
            "reset_rng_episode": False,
        }
        config.update({"movement_params": mparams})
        uparams = {"lower": -20, "upper": 20, "coeffs": (10, 0, 10)}
        config.update({"utility_params": uparams})

        # set up default configuration for tracked metrics
        config.update(
            {
                "metrics": {
                    "scalar_metrics": {},
                    "performance_metrics": {},
                    "system_metrics": {},
                    "ue_metrics": {},
                    "bs_metrics": {},
                    "ss_metrics": {},
                }
            }
        )

        return config

    @classmethod
    def seeding(cls, config):
        """Return config with updated and rotated seeds."""

        seed = config["seed"]
        keys = [
            "arrival_params",
            "channel_params",
            "scheduler_params",
            "movement_params",
            "utility_params",
        ]
        for num, key in enumerate(keys):
            if key not in config:
                config[key] = {}
            config[key]["seed"] = seed + num + 1

        return config

    def reset(self, *, seed=None, options=None):
        """Reset env to starting state. Return the initial obs and info."""
        super().reset(seed=seed)

        # reset time
        self.time = 0.0

        # set seed
        if seed is not None:
            self.seeding({"seed": seed})

        # initialize RNG or reset (if necessary on episode end)
        if self.reset_rng_episode or self.rng is None:
            self.rng = np.random.default_rng(self.seed)

        # extra options currently not supported
        if options is not None:
            raise NotImplementedError(
                "Passing extra options on env.reset() is not supported."
            )

        # reset state kept by arrival pattern, channel, scheduler, etc.
        self.arrival.reset()
        self.channel.reset()
        self.scheduler.reset()
        self.movement.reset()
        self.utility.reset()

        # reset scheduler last served index
        self.scheduler.reset()
        
        # reset UE, sensor, and base station queues
        for ue in self.users.values():
            ue.data_buffer_uplink.clear()

        for sensor in self.sensors.values():
            sensor.data_buffer_uplink.clear()

        for bs in self.stations.values():
            bs.transferred_jobs_ue.clear()
            bs.transferred_jobs_sensor.clear()
            bs.accomplished_jobs_ue.clear()
            bs.accomplished_jobs_sensor.clear()
            
        # call job generator reset to clear job data frames
        self.job_dataframe.reset_dataframes()
        
        # reset the job counter
        self.job_generator.job_counter = 0

        # generate new arrival and exit times for UEs
        for ue in self.users.values():
            ue.stime = self.arrival.arrival(ue)
            ue.extime = self.arrival.departure(ue)

        # generate new initial positons of UEs
        for ue in self.users.values():
            ue.x, ue.y = self.movement.initial_position(ue)

        # initially not all UEs request downlink connections (service)
        self.active = [ue for ue in self.users.values() if ue.stime <= 0]
        self.active = sorted(self.active, key=lambda ue: ue.ue_id)
        
        self.active_sensor = [sensor for sensor in self.sensors.values()]
        self.active_sensor = sorted(self.active_sensor, key=lambda sensor: sensor.sensor_id)

        # reset established downlink connections (default empty set)
        self.connections = defaultdict(set)
        # reset established downlink connections for sensors (default empty set)
        self.connections_sensor = defaultdict(set)
        # reset connections' data rates (defaults set to 0.0)
        self.datarates = defaultdict(float)
        # reset connections' data rates (defaults set to 0.0)
        self.datarates_sensor = defaultdict(float)
        # reset UEs' utilities
        self.utilities = {}
        # reset sensors utilities
        self.utilities_sensor = {}

        # reset resource allocations
        self.resource_allocations = {
            'bandwidth_ue': [],
            'bandwidth_sensor': [],
            'comp_power_ue': [],
            'comp_power_sensor': []
        }

        # reset aori and aosi
        self.aori_per_device = {}
        self.aosi_per_device = {}

        # reset traffic and computation requests
        self.traffic_requests_per_device = {}
        self.traffic_requests_per_sensor = {}
        self.computation_requests_per_device = {}
        self.computation_requests_per_sensor = {}

        # Reset episode reward
        self.timestep_reward = 0
        self.episode_reward = 0  

        # set time of last UE's departure
        self.max_departure = max(ue.extime for ue in self.users.values())

        # reset episode's results of metrics tracked by the monitor
        self.monitor.reset()

        # check if handler is applicable to mobile scenario
        self.handler.check(self)

        # info
        info = self.handler.info(self)
        # store latest monitored results in `info` dictionary
        info = {**info, **self.monitor.info()}

        # Return initial observation
        obs = self.handler.observation(self)

        return obs, info

    def apply_action(self, bs: BaseStation, bandwidth_allocation: float, computational_allocation: float) -> Tuple[float, float, float, float]:
        """Allocate bandwidth and computational resource between UEs and sensors."""
        if not (0 <= bandwidth_allocation <= 1 and 0 <= computational_allocation <= 1):
            raise ValueError("Allocations must be between 0 and 1")
        
        # Apply bandwidth resource allocation to UEs and sensors
        ue_bandwidth = bs.bw * bandwidth_allocation
        sensor_bandwidth = bs.bw * (1 - bandwidth_allocation)

        # Apply computational resource allocation to UEs and sensors
        ue_computational_power = bs.computational_power * computational_allocation
        sensor_computational_power = bs.computational_power * (1 - computational_allocation)

        # Store the allocation for this time step
        self.resource_allocations['bandwidth_ue'].append(bandwidth_allocation)
        self.resource_allocations['bandwidth_sensor'].append(1 - bandwidth_allocation)
        self.resource_allocations['comp_power_ue'].append(computational_allocation)
        self.resource_allocations['comp_power_sensor'].append(1 - computational_allocation)

        return ue_bandwidth, sensor_bandwidth, ue_computational_power, sensor_computational_power

    def check_connectivity(self, bs: BaseStation, ue: UserEquipment) -> bool:
        """Connection can be established if SNR exceeds threshold of UE."""
        snr = self.channel.snr(bs, ue)
        return snr > ue.snr_threshold
    
    def check_connectivity_sensor(self, bs: BaseStation, sensor: Sensor) -> bool:
        """Connection can be established if SNR exceeds threshold of sensor."""
        snr = self.channel.snr(bs, sensor)
        return snr > sensor.snr_threshold

    def available_connections(self, ue: UserEquipment) -> Set:
        """Returns set of what base stations users could connect to."""
        stations = self.stations.values()
        return {bs for bs in stations if self.check_connectivity(bs, ue)}

    def update_connections(self) -> None:
        """Release connections where BS and UE moved out-of-range."""
        connections = {
            bs: set(ue for ue in ues if self.check_connectivity(bs, ue))
            for bs, ues in self.connections.items()
        }
        self.connections.clear()
        self.connections.update(connections)

    def update_connections_sensors(self) -> None:
        """Release connections where BS and sensor is out-of-range."""
        connections_sensor = {
            bs: set(sensor for sensor in sensors if self.check_connectivity_sensor(bs, sensor))
            for bs, sensors in self.connections_sensor.items()
        }
        self.connections_sensor.clear()
        self.connections_sensor.update(connections_sensor)

    def connect_bs_ue(self) -> None:
        """Connect the UE to the closest base station within data range."""
        for ue in self.users.values():
            closest_bs: Optional[BaseStation] = None
            min_distance = float('inf')

            # Iterate through all base stations to find the closest one
            for bs in self.stations.values():
                distance = np.sqrt((ue.x - bs.x) ** 2 + (ue.y - bs.y) ** 2)
                if distance < min_distance:
                    min_distance = distance
                    closest_bs = bs

            # If a closest base station is found, establish the connection
            if closest_bs:
                if closest_bs not in self.connections:
                    self.connections[closest_bs] = set()
                self.connections[closest_bs].add(ue)

    def connect_bs_sensor(self) -> None:
        """Connect each sensor to the closest base station."""
        for sensor in self.sensors.values():
            closest_bs: Optional[BaseStation] = None
            min_distance = float('inf')
            
            # Iterate through all base stations to find the closest one
            for bs in self.stations.values():
                distance = np.sqrt((sensor.x - bs.x) ** 2 + (sensor.y - bs.y) ** 2)
                if distance < min_distance:
                    min_distance = distance
                    closest_bs = bs

            # If a closest base station is found, establish the connection
            if closest_bs:
                sensor.connected_base_station = closest_bs
                if closest_bs not in self.connections_sensor:
                    self.connections_sensor[closest_bs] = set()
                self.connections_sensor[closest_bs].add(sensor)

    def step(self, actions: Tuple[float, float]):
        assert not self.time_is_up, "step() called on terminated episode"

        # connect UEs and sensors to the closest base station and establish connection
        self.connect_bs_ue()
        self.connect_bs_sensor()

        # check snr thresholds, release established connections that moved e.g. out-of-range
        self.update_connections()
        self.update_connections_sensors()
        
        # log all connections
        #self.logger.log_connections(self)

        # generate jobs for each UE and sensor
        for ue in self.users.values():
            self.job_generator.generate_job_ue(ue)
        for sensor in self.sensors.values():
            self.job_generator.generate_job_sensor(sensor)

        # get traffic and computation requests from UEs and sensors
        self.get_traffic_request_ue()
        self.get_traffic_request_sensor()
        self.get_computation_request_ue()
        self.get_computation_request_sensor()

        # log sensor and ue data queues
        #self.logger.log_job_queues(self)

        # apply handler to transform actions to expected shape
        bw_split, comp_split = self.handler.action(self, actions)

        # apply the action to allocation of bs resources
        for bs in self.stations.values():
            bw_for_ues, bw_for_sensors, comp_power_for_ues, comp_power_for_sensors = self.apply_action(bs, bw_split, comp_split)
            
        # update connections' data rates after re-scheduling
        self.datarates = {}
        for bs in self.stations.values():
            drates_ue = self.station_allocation(bs, bw_for_ues)
            self.datarates.update(drates_ue)

        self.datarates_sensor = {}
        for bs in self.stations.values():
            drates_sensor = self.station_allocation_sensor(bs, bw_for_sensors)
            self.datarates_sensor.update(drates_sensor)

        # update macro (aggregated) data rates for each UE
        # this step is relevant if there is more than one base station
        self.macro = self.macro_datarates(self.datarates)
        self.macro_sensor = self.macro_datarates_sensor(self.datarates_sensor)

        # logging datarates
        #self.logger.log_datarates(self)

        # uplink data transmission
        self.job_transfer_manager.transfer_data_uplink()

        # log sensor and ue data queues
        #self.logger.log_job_queues(self)

        # process data in MEC servers
        self.job_process_manager.process_data_for_mec(comp_power_for_ues, comp_power_for_sensors)

        # log sensor and ue data queues
        #self.logger.log_job_queues(self)

        # compute the synchronization delays
        self.delay_manager.compute_absolute_synch_delay()

        # compute aori and aosi
        self.compute_aori()
        self.compute_aosi()

        # compute utilities from UEs' data rates & log its mean value
        self.utilities = {ue: self.utility.utility(self.macro[ue]) for ue in self.active}

        # scale utilities to range [-1, 1]
        self.utilities = {ue: self.utility.scale(util) for ue, util in self.utilities.items()}
        
        # compute utilities from sensor's data rates & log its mean value
        self.utilities_sensor = {sensor: self.utility.utility(self.macro_sensor[sensor]) for sensor in self.active_sensor}

        # scale utilities to range [-1, 1]
        self.utilities_sensor = {sensor: self.utility.scale(util) for sensor, util in self.utilities_sensor.items()}

        # compute rewards
        reward = self.handler.reward(self)
        self.timestep_reward = reward
        self.episode_reward += reward

        # log the job data frame
        self.job_dataframe.log_ue_packets()
        self.job_dataframe.log_sensor_packets()

        # evaluate metrics and update tracked metrics given the core simulation
        self.monitor.update(self)

        # move user equipments around; update positions of UEs
        for ue in self.active:
            ue.x, ue.y = self.movement.move(ue)

        # terminate existing connections for exiting UEs
        leaving = set([ue for ue in self.active if ue.extime <= self.time])
        for bs, ues in self.connections.items():
            self.connections[bs] = ues - leaving

        # update list of active UEs & add those that begin to request service
        self.active = sorted(
            [
                ue
                for ue in self.users.values()
                if ue.extime > self.time and ue.stime <= self.time
            ],
            key=lambda ue: ue.ue_id,
        )
        
        # update list of active sensors & add those that begin to request service
        self.active_sensor = sorted(
            [
                sensor
                for sensor in self.sensors.values()
            ],
            key=lambda sensor: sensor.sensor_id,
        )

        # check whether episode is done & close the environment
        if self.time_is_up and self.window:
            self.close()

        # do not invoke next step on policies before at least one UE is active
        if not self.active and not self.time_is_up:
            return self.step({})

        # compute observations for next step and information
        observation = self.handler.observation(self)
        
        # info
        info = self.handler.info(self)

        # store latest monitored results in `info` dictionary
        info = {**info, **self.monitor.info()}

        # update internal time of environment
        self.time += 1

        # there is not natural episode termination, just limited time
        # terminated is always False and truncated is True once time is up
        terminated = False
        truncated = self.time_is_up

        # If the episode ends, include the total reward
        if truncated: 
            info["episode reward"] = self.episode_reward

        return observation, reward, terminated, truncated, info

    @property
    def time_is_up(self):
        """Return true after max. time steps or once last UE departed."""
        return self.time >= min(self.EP_MAX_TIME, self.max_departure)

    def macro_datarates(self, datarates):
        """Compute aggregated UE data rates given all its connections to all base stations."""
        epsilon = 1e-10  # Small value to prevent zero data rates
        ue_datarates = Counter()
        for (bs, ue), datarate in self.datarates.items():
            ue_datarates.update({ue: datarate + epsilon})
        return ue_datarates
    
    def macro_datarates_sensor(self, datarates_sensor):
        """Compute aggregated sensor data rates given all its connections to all base stations."""
        epsilon = 1e-10  # Small value to prevent zero data rates
        sensor_datarates = Counter()
        for (bs, sensor), datarate in self.datarates_sensor.items():
            sensor_datarates.update({sensor: datarate + epsilon})
        return sensor_datarates

    def station_allocation(self, bs: BaseStation, bandwidth_for_ues: float) -> Dict:
        """Schedule BS's resources (e.g. phy. res. blocks) to connected UEs."""
        conns = sorted(self.active, key=lambda ue: ue.ue_id)

        # Compute SNR for each connected user equipment
        snrs = [self.channel.snr(bs, ue) for ue in conns]

        # BS shares resources among connected user equipments
        scheduled_bw = self.scheduler.share_ue(bs, conns, bandwidth_for_ues)

        # UE's max. data rate achievable when BS schedules resources to it
        data_rate_alloc = [self.channel.data_rate(ue, bw, snr) for ue, bw, snr in zip(conns, scheduled_bw, snrs)]

        return {(bs, ue): rate for ue, rate in zip(conns, data_rate_alloc)}
    
    def station_allocation_sensor(self, bs: BaseStation, bandwidth_for_sensors: float) -> Dict:
        """Schedule BS's resources (e.g. phy. res. blocks) to connected sensors."""
        conns_sensors = sorted(self.active_sensor, key=lambda sensor: sensor.sensor_id)

        # Compute SNR for each connected sensor
        snrs = [self.channel.snr(bs, sensor) for sensor in conns_sensors]

        # BS shares resources among connected user equipments
        scheduled_bw = self.scheduler.share_sensor(bs, conns_sensors, bandwidth_for_sensors)

        # UE's max. data rate achievable when BS schedules resources to it
        data_rate_alloc = [self.channel.data_rate(sensor, bw, snr) for sensor, bw, snr in zip(conns_sensors, scheduled_bw, snrs)]

        return {(bs, sensor): rate for sensor, rate in zip(conns_sensors, data_rate_alloc)}


    def station_utilities(self) -> Dict[BaseStation, UserEquipment]:
        """Compute average utility of UEs connected to the basestation."""
        # set utility of BS with no active connections (idle BS) to
        # (scaled) lower utility bound
        idle = self.utility.scale(self.utility.lower)

        util = {
            bs: sum(self.utilities[ue] for ue in self.connections[bs])
            / len(self.connections[bs])
            if self.connections[bs]
            else idle
            for bs in self.stations.values()
        }

        return util
    
    def station_utilities_sensor(self) -> Dict[BaseStation, Sensor]:
        """Compute average utility of sensors connected to the basestation."""
        # set utility of BS with no active connections (idle BS) to
        # (scaled) lower utility bound
        idle = self.utility_sensor.scale(self.utility.lower)

        util = {
            bs: sum(self.utilities_sensor[sensor] for sensor in self.connections_sensor[bs])
            / len(self.connections_sensor[bs])
            if self.connections_sensor[bs]
            else idle
            for bs in self.stations.values()
        }

        return util


    def get_traffic_request_ue(self):
        """Get the traffic request from all UEs at the current timestep."""
        self.traffic_requests_per_device = {ue.ue_id: ue.total_traffic_request for ue in self.users.values()}
        return self.traffic_requests_per_device

    def get_traffic_request_sensor(self):
        """Get the traffic request from all sensors at the current timestep."""
        self.traffic_requests_per_sensor = {sensor.sensor_id: sensor.total_traffic_request for sensor in self.sensors.values()}
        return self.traffic_requests_per_sensor

    def get_computation_request_ue(self):
        """Get the computation request from all UEs at the current timestep."""
        self.computation_requests_per_device = {ue.ue_id: ue.total_computation_request for ue in self.users.values()}
        return self.computation_requests_per_device

    def get_computation_request_sensor(self):
        """Get the computation request from all sensors at the current timestep."""
        self.computation_requests_per_sensor = {sensor.sensor_id: sensor.total_computation_request for sensor in self.sensors.values()}
        return self.computation_requests_per_sensor


    def compute_aori(self) -> Dict[int, Optional[float]]:
        """Compute AoRI (Age of Request Information) for all accomplished packets at the current timestep."""
        # TODO: handling missing data -> what can we put if there is no accomplished packets? None?
        # TODO: is sum the best aggregation way? -> can we use max or mean?
        self.aori_per_device = {ue.ue_id: None for ue in self.users.values()}

        accomplished_packets = self.job_dataframe.df_ue_packets[
            (self.job_dataframe.df_ue_packets['accomplished_time'] == self.time)
        ].copy()
        
        if not accomplished_packets.empty:
            # Group accomplished packets by device ID, sum the e2e delay for each device, convert to dictionary
            aori_logs_per_user = (accomplished_packets
                .groupby('device_id')
                .agg({'e2e_delay': 'sum'})
                .to_dict()['e2e_delay'])
            
            # Update only the values that exist
            self.aori_per_device.update(aori_logs_per_user)

        return self.aori_per_device
    
    def compute_aosi(self) -> Dict:
        """Compute AoSI for all accomplished UE packets at the current timestep."""
        # TODO: is sum the best aggregation way? -> can we use max or mean?
        self.aosi_per_device = {ue.ue_id: None for ue in self.users.values()}

        accomplished_packets = self.job_dataframe.df_ue_packets[
            (self.job_dataframe.df_ue_packets['synch_reward'].isnull())
        ].copy()

        if not accomplished_packets.empty:
            # Group accomplished packets by device ID, sum the synch delay for each device, convert to dictionary
            aosi_logs_per_user = (accomplished_packets
                .groupby('device_id')
                .agg({'synch_delay': 'sum'})
                .to_dict()['synch_delay'])
            
            # Update only the values that exist
            self.aosi_per_device.update(aosi_logs_per_user)

        return self.aosi_per_device


    def bs_isolines(self, drate: float) -> Dict:
        """Isolines where UEs could still receive `drate` max. data rate."""
        isolines = {}
        config = self.default_config()["ue"]

        for bs in self.stations.values():
            isolines[bs] = self.channel.isoline(
                bs, config, (self.width, self.height), drate
            )

        return isolines

    def render(self) -> None:
        mode = self.render_mode

        # do not continue rendering once environment has been closed
        if self.closed:
            return

        # calculate isoline contours for BSs' connectivity range
        if self.conn_isolines is None:
            self.conn_isolines = self.bs_isolines(1000000000.0)
        # calculate isoline contours for BS range
        if self.mb_isolines is None:
            self.mb_isolines = self.bs_isolines(5000000000.0)

        # set up matplotlib figure & axis configuration
        fig = plt.figure()
        fx = max(3.0 / 2.0 * 1.25 * self.width / fig.dpi, 8.0)
        fy = max(1.25 * self.height / fig.dpi, 5.0)
        plt.close()
        fig = plt.figure(figsize=(fx, fy))
        gs = fig.add_gridspec(
            ncols=2,
            nrows=3,
            width_ratios=(4, 2),
            height_ratios=(2, 3, 3),
            hspace=0.45,
            wspace=0.2,
            top=0.95,
            bottom=0.15,
            left=0.025,
            right=0.955,
        )

        sim_ax = fig.add_subplot(gs[:, 0])
        dash_ax = fig.add_subplot(gs[0, 1])
        qoe_ax = fig.add_subplot(gs[1, 1])
        conn_ax = fig.add_subplot(
            gs[2, 1],
        )

        # render simulation, metrics and score if step() was called
        # i.e. this prevents rendering in the sequential environment before
        # the first round-robin of actions is finalized
        if self.time > 0:
            self.render_simulation(sim_ax)
            self.render_dashboard(dash_ax)
            self.render_bandwidth_allocation(qoe_ax)
            self.render_computation_allocation(conn_ax)

        # align plots' y-axis labels
        fig.align_ylabels((qoe_ax, conn_ax))
        canvas = FigureCanvas(fig)
        canvas.draw()

        # prevents opening multiple figures on consecutive render() calls
        plt.close()

        if mode == "rgb_array":
            # render RGB image for e.g. video recording
            data = np.frombuffer(canvas.tostring_rgb(), dtype=np.uint8)
            # reshape image from 1d array to 2d array
            return data.reshape(canvas.get_width_height()[::-1] + (3,))

        elif mode == "human":
            # render RGBA image on pygame surface
            data = canvas.buffer_rgba()
            size = canvas.get_width_height()

            # set up pygame window to display matplotlib figure
            if self.window is None:
                pygame.init()
                self.clock = pygame.time.Clock()

                # set window size to figure's size in pixels
                window_size = tuple(map(int, fig.get_size_inches() * fig.dpi))
                self.window = pygame.display.set_mode(window_size)

                # remove pygame icon from window; set icon to empty surface
                pygame.display.set_icon(Surface((0, 0)))

                # set window's caption and background color
                pygame.display.set_caption("MComEnv")

            # clear surface
            self.window.fill("white")

            # plot matplotlib's RGBA frame on the pygame surface
            screen = pygame.display.get_surface()
            plot = pygame.image.frombuffer(data, size, "RGBA")
            screen.blit(plot, (0, 0))

            # update the full display surface to the window
            pygame.display.flip()

            # handle pygame events (such as closing the window)
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.close()

        else:
            raise ValueError("Invalid rendering mode.")

    def render_simulation(self, ax) -> None:
        colormap = cm.get_cmap("RdYlGn")
        # define normalization for unscaled utilities
        unorm = plt.Normalize(self.utility.lower, self.utility.upper)

        for ue, utility in self.utilities.items():
            # plot UE by its (unscaled) utility
            utility = self.utility.unscale(utility)
            color = colormap(unorm(utility))

            ax.scatter(
                ue.point.x,
                ue.point.y,
                s=200,
                zorder=2,
                color=color,
                marker="o",
            )
            ax.annotate(ue.ue_id, xy=(ue.point.x, ue.point.y), ha="center", va="center")

        for bs in self.stations.values():
            # plot BS symbol and annonate by its BS ID
            ax.plot(
                bs.point.x,
                bs.point.y,
                marker=BS_SYMBOL,
                markersize=30,
                markeredgewidth=0.1,
                color="black",
            )
            bs_id = string.ascii_uppercase[bs.bs_id]
            ax.annotate(
                bs_id,
                xy=(bs.point.x, bs.point.y),
                xytext=(0, -25),
                ha="center",
                va="bottom",
                textcoords="offset points",
            )

            # plot BS ranges where UEs may connect or can receive at most 1MB/s
            ax.scatter(*self.conn_isolines[bs], color="gray", s=3)
            ax.scatter(*self.mb_isolines[bs], color="black", s=3)

        for bs in self.stations.values():
            for ue in self.connections[bs]:
                # color is connection's contribution to the UE's total utility
                share = self.datarates[(bs, ue)] / self.macro[ue]
                share = share * self.utility.unscale(self.utilities[ue])
                color = colormap(unorm(share))

                # add black background/borders for lines for visibility
                ax.plot(
                    [ue.point.x, bs.point.x],
                    [ue.point.y, bs.point.y],
                    color=color,
                    path_effects=[
                        pe.SimpleLineShadow(shadow_color="black"),
                        pe.Normal(),
                    ],
                    linewidth=3,
                    zorder=-1,
                )

        for sensor in self.sensors.values():
            # plot sensor symbol and annonate by its sensor ID
            ax.plot(
                sensor.point.x,
                sensor.point.y,
                marker=SENSOR_SYMBOL,
                markersize=10,
                markeredgewidth=0.1,
                color="blue",
            )
            sensor_id = string.ascii_uppercase[sensor.sensor_id]
            ax.annotate(
                sensor_id,
                xy=(sensor.point.x, sensor.point.y),
                xytext=(0, -15),
                ha="center",
                va="bottom",
                textcoords="offset points",
                fontsize="8",
            )

        # remove simulation axis's ticks and spines
        ax.get_xaxis().set_visible(False)
        ax.get_yaxis().set_visible(False)

        ax.spines["top"].set_visible(False)
        ax.spines["bottom"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_visible(False)

        ax.set_xlim([0, self.width])
        ax.set_ylim([0, self.height])

    def render_dashboard(self, ax) -> None:
        mean_aoris = self.monitor.performance_results["total aori"]
        mean_aori = mean_aoris[-1]
        total_mean_aori = np.mean(mean_aoris)

        mean_aosis = self.monitor.performance_results["total aosi"]
        mean_aosi = mean_aosis[-1]
        total_mean_aosi = np.mean(mean_aosis)

        # remove simulation axis's ticks and spines
        ax.get_xaxis().set_visible(False)
        ax.get_yaxis().set_visible(False)

        ax.spines["top"].set_visible(False)
        ax.spines["bottom"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_visible(False)

        rows = ["Current", "History"]
        cols = ["AoRI", "AoSI"]
        text = [
            [f"{mean_aori:.2f}", f"{mean_aosi:.2f}"],
            [f"{total_mean_aori:.2f}", f"{total_mean_aosi:.2f}"],
        ]

        table = ax.table(
            text,
            rowLabels=rows,
            colLabels=cols,
            cellLoc="center",
            edges="B",
            loc="upper center",
            bbox=[0.0, -0.25, 1.0, 1.25],
        )
        table.auto_set_font_size(False)
        table.set_fontsize(11)

    def render_bandwidth_allocation(self, ax) -> None:
        time = np.arange(self.time)
        bw_ue = self.monitor.performance_results["bw allocation UE"]
        ax.plot(time, bw_ue, linewidth=1, color="black")

        ax.set_xlabel("Time")
        ax.set_ylabel("BW Alloc. UE")
        ax.set_xlim([0.0, self.EP_MAX_TIME])
        ax.set_ylim([0.0, 1.0])

    def render_computation_allocation(self, ax) -> None:
        time = np.arange(self.time)
        comp_ue = self.monitor.performance_results["comp. allocation UE"]
        ax.plot(time, comp_ue, linewidth=1, color="black")

        ax.set_xlabel("Time")
        ax.set_ylabel("Comp. Alloc. Sensor")
        ax.set_xlim([0.0, self.EP_MAX_TIME])
        ax.set_ylim([0.0, 1.0])


    def close(self) -> None:
        """Closes the environment and terminates its visualization."""
        pygame.quit()
        self.window = None
        self.closed = True