# Z-Probe support
#
# Copyright (C) 2017-2024  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import pins

direction_types = {'x+': [0, +1], 'x-': [0, -1], 'y+': [1, +1], 'y-': [1, -1], 'z+': [2, +1], 'z-': [2, -1]}

HINT_TIMEOUT = """
If the probe did not move far enough to trigger, then
consider reducing the Z axis minimum position so the probe
can travel further (the Z minimum position can be negative).
"""

# Calculate the average Z from a set of positions
def calc_probe_average(positions, method='average'):
    if method != 'median':
        # Use mean average
        count = float(len(positions))
        return [sum([pos[i] for pos in positions]) / count
                for i in range(3)]
    # Use median
    z_sorted = sorted(positions, key=(lambda p: p[2]))
    middle = len(positions) // 2
    if (len(positions) & 1) == 1:
        # odd number of samples
        return z_sorted[middle]
    # even number of samples
    return calc_probe_average(z_sorted[middle-1:middle+1], 'average')


######################################################################
# Probe device implementation helpers
######################################################################

# Helper to implement common probing commands
class ProbeCommandHelper:
    def __init__(self, config, probe, query_endstop=None):
        self.printer = config.get_printer()
        self.probe = probe
        self.query_endstop = query_endstop
        self.name = config.get_name()
        gcode = self.printer.lookup_object('gcode')
        # QUERY_PROBE command
        self.last_state = False
        gcode.register_command('QUERY_PROBE', self.cmd_QUERY_PROBE,
                               desc=self.cmd_QUERY_PROBE_help)
        # PROBE command
        self.last_z_result = 0.
        gcode.register_command('PROBE', self.cmd_PROBE,
                               desc=self.cmd_PROBE_help)

        # Other commands
        gcode.register_command('PROBE_ACCURACY', self.cmd_PROBE_ACCURACY,
                               desc=self.cmd_PROBE_ACCURACY_help)

    def _move(self, coord, speed):
        self.printer.lookup_object('toolhead').manual_move(coord, speed)

    def get_status(self, eventtime):
        return {'name': self.name,
                'last_query': self.last_state,
                'last_z_result': self.last_z_result}

    cmd_QUERY_PROBE_help = "Return the status of the z-probe"
    def cmd_QUERY_PROBE(self, gcmd):
        if self.query_endstop is None:
            raise gcmd.error("Probe does not support QUERY_PROBE")
        toolhead = self.printer.lookup_object('toolhead')
        print_time = toolhead.get_last_move_time()
        res = self.query_endstop(print_time)
        self.last_state = res
        gcmd.respond_info("probe: %s" % (["open", "TRIGGERED"][not not res],))

    cmd_PROBE_help = "Probe Z-height at current XY position"
    def cmd_PROBE(self, gcmd):
        direction = gcmd.get("DIRECTION", 'z-')
        pos = run_single_probe(self.probe, gcmd, direction)
        gcmd.respond_info(f"Result is {pos[0]}, {pos[1]}, {pos[2]}")
        self.last_z_result = pos[2]

    cmd_PROBE_ACCURACY_help = "Probe Z-height accuracy at current XY position"
    def cmd_PROBE_ACCURACY(self, gcmd):
        params = self.probe.get_probe_params(gcmd)
        direction = gcmd.get("DIRECTION", 'z-')
        (axis, sense) = direction_types[direction]
        sample_count = gcmd.get_int("SAMPLES", 10, minval=1)
        toolhead = self.printer.lookup_object('toolhead')
        pos = toolhead.get_position()
        gcmd.respond_info("PROBE_ACCURACY at X:%.3f Y:%.3f Z:%.3f"
                          " (samples=%d retract=%.3f"
                          " speed=%.1f lift_speed=%.1f)\n"
                          % (pos[0], pos[1], pos[2],
                             sample_count, params['sample_retract_dist'],
                             params['probe_speed'], params['lift_speed']))
        # Create dummy gcmd with SAMPLES=1
        fo_params = dict(gcmd.get_command_parameters())
        fo_params['SAMPLES'] = '1'
        gcode = self.printer.lookup_object('gcode')
        fo_gcmd = gcode.create_gcode_command("", "", fo_params)
        # Probe bed sample_count times
        probe_session = self.probe.start_probe_session(fo_gcmd, direction)
        probe_num = 0
        while probe_num < sample_count:
            # Probe position
            probe_session.run_probe(fo_gcmd, direction)
            probe_num += 1
            # Retract
            pos = toolhead.get_position()
            liftpos = pos
            liftpos[axis] = pos[axis] - sense * params['sample_retract_dist']
            toolhead.manual_move(liftpos, params['lift_speed'])
            gcmd.respond_info(f'finished sample {probe_num} of {sample_count}')
        gcmd.respond_info(f'pulling probed_results')
        positions = probe_session.pull_probed_results()
        gcmd.respond_info(f'probed_results {positions[0]}, {positions[1]}, {positions[2]}')
        probe_session.end_probe_session(direction)
        # Calculate maximum, minimum and average values
        max_value = max([p[axis] for p in positions])
        min_value = min([p[axis] for p in positions])
        range_value = max_value - min_value
        avg_value = calc_probe_average(positions, 'average')[axis]
        median = calc_probe_average(positions, 'median')[axis]
        # calculate the standard deviation
        deviation_sum = 0
        for i in range(len(positions)):
            deviation_sum += pow(positions[i][2] - avg_value, 2.)
        sigma = (deviation_sum / len(positions)) ** 0.5
        # Show information
        gcmd.respond_info(
            "probe accuracy results: maximum %.6f, minimum %.6f, range %.6f, "
            "average %.6f, median %.6f, standard deviation %.6f" % (
            max_value, min_value, range_value, avg_value, median, sigma))

# Helper to track multiple probe attempts in a single command
class ProbeSessionHelper:
    def __init__(self, config, mcu_probes):
        self.printer = config.get_printer()
        self.mcu_probes = mcu_probes
        gcode = self.printer.lookup_object('gcode')
        self.dummy_gcode_cmd = gcode.create_gcode_command("", "", {})

        # Configurable probing speeds
        self.speed = config.getfloat('speed', 5.0, above=0.)
        self.lift_speed = config.getfloat('lift_speed', self.speed, above=0.)
        # Bouncing support (for improved accuracy)
        self.max_distance = config.getfloat('max_distance', 10.0, above=0.)
        # Multi-sample support (for improved accuracy)
        self.sample_count = config.getint('samples', 1, minval=1)
        self.sample_retract_dist = config.getfloat('sample_retract_dist', 2.,
                                                   above=0.)
        atypes = ['median', 'average']
        self.samples_result = config.getchoice('samples_result', atypes, 'average')
        self.samples_tolerance = config.getfloat('samples_tolerance', 0.100, minval=0.)
        self.samples_retries = config.getint('samples_tolerance_retries', 0, minval=0)
        # Session state
        self.multi_probe_pending = False
        self.results = []
        # Register event handlers
        self.printer.register_event_handler("gcode:command_error",
                                            self._handle_command_error)
    def _handle_command_error(self):
        if self.multi_probe_pending:
            try:
                self.end_probe_session()
            except:
                logging.exception("Multi-probe end")
    def _probe_state_error(self):
        raise self.printer.command_error(
            "Internal probe error - start/end probe session mismatch")
    def start_probe_session(self, gcmd, direction='z-'):
        if self.multi_probe_pending:
            self._probe_state_error()
        (axis, sense) = direction_types[direction]
        self.mcu_probes[axis].multi_probe_begin()
        self.multi_probe_pending = True
        self.results = []
        return self
    def end_probe_session(self, direction='z-'):
        if not self.multi_probe_pending:
            self._probe_state_error()
        (axis, sense) = direction_types[direction]
        self.results = []
        self.multi_probe_pending = False
        self.mcu_probes[axis].multi_probe_end()
    def get_probe_params(self, gcmd=None):
        if gcmd is None:
            gcmd = self.dummy_gcode_cmd
        probe_speed = gcmd.get_float("PROBE_SPEED", self.speed, above=0.)
        max_distance = gcmd.get_float("MAX_DISTANCE", self.max_distance, above=0.)
        lift_speed = gcmd.get_float("LIFT_SPEED", self.lift_speed, above=0.)
        samples = gcmd.get_int("SAMPLES", self.sample_count, minval=1)
        sample_retract_dist = gcmd.get_float("SAMPLE_RETRACT_DIST",
                                             self.sample_retract_dist, above=0.)
        samples_tolerance = gcmd.get_float("SAMPLES_TOLERANCE",
                                           self.samples_tolerance, minval=0.)
        samples_retries = gcmd.get_int("SAMPLES_TOLERANCE_RETRIES",
                                       self.samples_retries, minval=0)
        samples_result = gcmd.get("SAMPLES_RESULT", self.samples_result)
        return {'probe_speed': probe_speed,
                'lift_speed': lift_speed,
                'max_distance': max_distance,
                'samples': samples,
                'sample_retract_dist': sample_retract_dist,
                'samples_tolerance': samples_tolerance,
                'samples_tolerance_retries': samples_retries,
                'samples_result': samples_result}

    def run_probe(self, gcmd, direction='z-'):
        if not self.multi_probe_pending:
            self._probe_state_error()
        params = self.get_probe_params(gcmd)
        if direction not in direction_types:
            raise self.printer.command_error("Wrong value for DIRECTION.")
        logging.info("run_probe direction = " + str(direction))
        (axis, sense) = direction_types[direction]
        logging.info("run_probe axis = %d, sense = %d" % (axis, sense))
        gcode = self.printer.lookup_object('gcode')
        gcode.respond_info(f"Probing {axis} axis with {sense} sense")
        toolhead = self.printer.lookup_object('toolhead')
        start_position = self.printer.lookup_object('toolhead').get_position()
        retries = 0
        positions = []
        sample_count = params['samples']
        while len(positions) < sample_count:
            # Probe position
            pos = self._bouncing_probe(params['probe_speed'], direction)
            positions.append(pos)
            # Check samples tolerance
            axis_positions = [p[axis] for p in positions]
            if max(axis_positions)-min(axis_positions) > params['samples_tolerance']:
                if retries >= params['samples_tolerance_retries']:
                    raise gcmd.error("Probe samples exceed samples_tolerance")
                gcmd.respond_info("Probe samples exceed tolerance. Retrying...")
                retries += 1
                positions = []
            # Retract
            if len(positions) < sample_count:
                liftpos = start_position
                liftpos[axis] = pos[axis] - sense * params['sample_retract_dist']
                toolhead.manual_move(liftpos, params['lift_speed'])

        # Calculate result
        result_position = self._calculate_results(positions, params['samples_result'], axis)
        self.results.append(result_position)

    def _bouncing_probe(self, speed, direction='z-'):
        toolhead = self.printer.lookup_object('toolhead')
        gcode = self.printer.lookup_object('gcode')
        probe_start = toolhead.get_position()
        (axis, sense) = direction_types[direction]
        bounce_count = 3
        bounces = 0
        bouncing_speed = speed
        bouncing_lift_speed = bouncing_speed * 2.0
        while bounces < bounce_count:
            pos = self._probe(bouncing_speed, direction)
            bouncing_speed = bouncing_speed * 0.1
            bouncing_retract_dist = bouncing_speed * 3.0
            liftpos = probe_start
            liftpos[axis] = pos[axis] - sense * bouncing_retract_dist
            toolhead.manual_move(liftpos, bouncing_lift_speed)
            bounces += 1
        # Allow axis_twist_compensation to update results
        self.printer.send_event("probe:update_results", pos)
        gcode.respond_info(f"Probe made contact in {direction} direction at {pos[0]},{pos[1]},{pos[2]}")
        return pos

    def _probe(self, speed, direction='z-'):
        self.check_homed()
        (axis, sense) = direction_types[direction]
        pos = self._get_target_position(direction)
        try:
            epos = self.mcu_probes[axis].probing_move(pos, speed)
        except self.printer.command_error as e:
            reason = str(e)
            if "Timeout during endstop homing" in reason:
                reason += HINT_TIMEOUT
            raise self.printer.command_error(reason)
        return epos[:3]

    def check_homed(self):
        toolhead = self.printer.lookup_object('toolhead')
        curtime = self.printer.get_reactor().monotonic()
        if 'x' not in toolhead.get_status(curtime)['homed_axes'] or \
                'y' not in toolhead.get_status(curtime)['homed_axes'] or \
                'z' not in toolhead.get_status(curtime)['homed_axes']:
            raise self.printer.command_error("Must home before probe")
        
    def _get_target_position(self, direction):
        toolhead = self.printer.lookup_object('toolhead')
        curtime = self.printer.get_reactor().monotonic()
        (axis, sense) = direction_types[direction]
        pos = toolhead.get_position()
        kin_status = toolhead.get_kinematics().get_status(curtime)
        if 'axis_minimum' not in kin_status or 'axis_minimum' not in kin_status:
            raise self.gcode.error(
                "Tools calibrate only works with cartesian kinematics")
        if sense > 0:
            pos[axis] = min(pos[axis] + self.max_distance,
                            kin_status['axis_maximum'][axis])
        else:
            pos[axis] = max(pos[axis] - self.max_distance,
                            kin_status['axis_minimum'][axis])
        return pos

    def _calculate_results(self, positions, samples_result, axis):
        if samples_result == 'median':
            return self._calc_median(positions, axis)
        return self._calc_mean(positions)

    def _calc_mean(self, positions):
        count = float(len(positions))
        return [sum([pos[i] for pos in positions]) / count
                for i in range(3)]

    def _calc_median(self, positions, axis):
        axis_sorted = sorted(positions, key=(lambda p: p[axis]))
        middle = len(positions) // 2
        if (len(positions) & 1) == 1:
            # odd number of samples
            return axis_sorted[middle]
        # even number of samples
        return self._calc_mean(axis_sorted[middle - 1:middle + 1])

    def pull_probed_results(self):
        res = self.results
        self.results = []
        return res

# Helper to read the xyz probe offsets from the config
class ProbeOffsetsHelper:
    def __init__(self, config):
        self.x_offset = config.getfloat('x_offset', 0.)
        self.y_offset = config.getfloat('y_offset', 0.)
        self.z_offset = config.getfloat('z_offset')
    def get_offsets(self):
        return self.x_offset, self.y_offset, self.z_offset


######################################################################
# Tools for utilizing the probe
######################################################################

# Helper code that can probe a series of points and report the
# position at each point.
class ProbePointsHelper:
    def __init__(self, config, finalize_callback, default_points=None):
        self.printer = config.get_printer()
        self.finalize_callback = finalize_callback
        self.probe_points = default_points
        self.name = config.get_name()
        self.gcode = self.printer.lookup_object('gcode')
        # Read config settings
        if default_points is None or config.get('points', None) is not None:
            self.probe_points = config.getlists('points', seps=(',', '\n'),
                                                parser=float, count=2)
        def_move_z = config.getfloat('horizontal_move_z', 5.)
        self.default_horizontal_move_z = def_move_z
        self.speed = config.getfloat('speed', 50., above=0.)
        self.use_offsets = False
        # Internal probing state
        self.lift_speed = self.speed
        self.probe_offsets = (0., 0., 0.)
        self.manual_results = []
    def minimum_points(self,n):
        if len(self.probe_points) < n:
            raise self.printer.config_error(
                "Need at least %d probe points for %s" % (n, self.name))
    def update_probe_points(self, points, min_points):
        self.probe_points = points
        self.minimum_points(min_points)
    def use_xy_offsets(self, use_offsets):
        self.use_offsets = use_offsets
    def get_lift_speed(self):
        return self.lift_speed
    def _move(self, coord, speed):
        self.printer.lookup_object('toolhead').manual_move(coord, speed)
    def _raise_tool(self, is_first=False):
        speed = self.lift_speed
        if is_first:
            # Use full speed to first probe position
            speed = self.speed
        self._move([None, None, self.horizontal_move_z], speed)
    def _invoke_callback(self, results):
        # Flush lookahead queue
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.get_last_move_time()
        # Invoke callback
        res = self.finalize_callback(self.probe_offsets, results)
        return res != "retry"
    def _move_next(self, probe_num):
        # Move to next XY probe point
        nextpos = list(self.probe_points[probe_num])
        if self.use_offsets:
            nextpos[0] -= self.probe_offsets[0]
            nextpos[1] -= self.probe_offsets[1]
        self._move(nextpos, self.speed)
    def start_probe(self, gcmd):
        # Lookup objects
        probe = self.printer.lookup_object('probe', None)
        method = gcmd.get('METHOD', 'automatic').lower()
        def_move_z = self.default_horizontal_move_z
        self.horizontal_move_z = gcmd.get_float('HORIZONTAL_MOVE_Z', def_move_z)
        # Perform automatic probing
        self.lift_speed = probe.get_probe_params(gcmd)['lift_speed']
        self.probe_offsets = probe.get_offsets()
        if self.horizontal_move_z < self.probe_offsets[2]:
            raise gcmd.error("horizontal_move_z can't be less than probe's z_offset")
        probe_session = probe.start_probe_session(gcmd)
        probe_num = 0
        while 1:
            self._raise_tool(not probe_num)
            if probe_num >= len(self.probe_points):
                results = probe_session.pull_probed_results()
                done = self._invoke_callback(results)
                if done:
                    break
                # Caller wants a "retry" - restart probing
                probe_num = 0
            self._move_next(probe_num)
            probe_session.run_probe(gcmd)
            probe_num += 1
        probe_session.end_probe_session()

# Helper to obtain a single probe measurement
def run_single_probe(probe, gcmd, direction='z-'):
    probe_session = probe.start_probe_session(gcmd)
    probe_session.run_probe(gcmd, direction)
    pos = probe_session.pull_probed_results()[0]
    probe_session.end_probe_session()
    return pos


######################################################################
# Handle [probe] config
######################################################################

# Endstop wrapper that enables probe specific features
class ProbeEndstopWrapper:
    def __init__(self, config, axis_name):
        self.printer = config.get_printer()
        self.axis_name = axis_name
        self.position_endstop = 0.
        self.stow_on_each_sample = config.getboolean('deactivate_on_each_sample', True)
        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.activate_gcode = gcode_macro.load_template(config, 'activate_gcode', '')
        self.deactivate_gcode = gcode_macro.load_template(config, 'deactivate_gcode', '')
        # Create an "endstop" object to handle the probe pin
        ppins = self.printer.lookup_object('pins')
        self.mcu_endstop = ppins.setup_pin('endstop', config.get('pin'))
        self.printer.register_event_handler('klippy:mcu_identify', self._handle_mcu_identify)
        # Wrappers
        self.get_mcu = self.mcu_endstop.get_mcu
        self.add_stepper = self.mcu_endstop.add_stepper
        self.get_steppers = self.mcu_endstop.get_steppers
        self.home_start = self.mcu_endstop.home_start
        self.home_wait = self.mcu_endstop.home_wait
        self.query_endstop = self.mcu_endstop.query_endstop
        # multi probes state
        self.multi = 'OFF'
    def _raise_probe(self):
        toolhead = self.printer.lookup_object('toolhead')
        start_pos = toolhead.get_position()
        self.deactivate_gcode.run_gcode_from_command()
        if toolhead.get_position()[:3] != start_pos[:3]:
            raise self.printer.command_error(
                "Toolhead moved during probe deactivate_gcode script")
    def _lower_probe(self):
        toolhead = self.printer.lookup_object('toolhead')
        start_pos = toolhead.get_position()
        self.activate_gcode.run_gcode_from_command()
        if toolhead.get_position()[:3] != start_pos[:3]:
            raise self.printer.command_error(
                "Toolhead moved during probe activate_gcode script")
    def multi_probe_begin(self):
        if self.stow_on_each_sample:
            return
        self.multi = 'FIRST'
    def multi_probe_end(self):
        if self.stow_on_each_sample:
            return
        self._raise_probe()
        self.multi = 'OFF'
    def probing_move(self, pos, speed):
        gcode = self.printer.lookup_object('gcode')
        phoming = self.printer.lookup_object('homing')
        return phoming.probing_move(self, pos, speed)
    def probe_prepare(self, hmove):
        if self.multi == 'OFF' or self.multi == 'FIRST':
            self._lower_probe()
            if self.multi == 'FIRST':
                self.multi = 'ON'
    def probe_finish(self, hmove):
        if self.multi == 'OFF':
            self._raise_probe()
    def get_position_endstop(self):
        return self.position_endstop

    def _handle_mcu_identify(self):
        kin = self.printer.lookup_object('toolhead').get_kinematics()
        for stepper in kin.get_steppers():
            if stepper.is_active_axis(self.axis_name):
                self.add_stepper(stepper)

# Main external probe interface
class MultiAxisBouncingProbe:
    def __init__(self, config):
        self.name = config.get_name()
        self.printer = config.get_printer()

        pin = config.get('pin')
        ppins = self.printer.lookup_object('pins')
        ppins.allow_multi_use_pin(pin.replace('^', '').replace('!', '').replace('~', ''))

        self.mcu_probes = [
            ProbeEndstopWrapper(config, 'x'),
            ProbeEndstopWrapper(config, 'y'),
            ProbeEndstopWrapper(config, 'z')
        ]
        self.cmd_helper = ProbeCommandHelper(config, self, self.mcu_probes[2].query_endstop)
        self.probe_offsets = ProbeOffsetsHelper(config)
        self.probe_session = ProbeSessionHelper(config, self.mcu_probes)
        self.printer.add_object('probe', self)
        self.printer.add_object(self.name, self)


    def get_probe_params(self, gcmd=None):
        return self.probe_session.get_probe_params(gcmd)

    def get_offsets(self):
        return self.probe_offsets.get_offsets()

    def get_status(self, eventtime):
        return self.cmd_helper.get_status(eventtime)

    def start_probe_session(self, gcmd, direction='z-'):
        return self.probe_session.start_probe_session(gcmd, direction)
    

def load_config(config):
    return MultiAxisBouncingProbe(config)