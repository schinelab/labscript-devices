import numpy as np
import labscript_utils.h5_lock
import h5py
from blacs.tab_base_classes import Worker
from labscript import LabscriptError

import sys
import os
import logging
import time
import datetime
from zprocess import RichStreamHandler
import zprocess
import inspect

Hz_per_lsb = 614.4 * 10**6 / (2**32)
lsb_per_MHz = np.round(1e6 / Hz_per_lsb).astype(int)
one_sixth_MHz_lsb = lsb_per_MHz // 6

class BlueBoxWorker(Worker):
    # additional properties passed down by the initialise_workers() method in blacs_tabs.py
    # - build_path
    # - vivado_path
    # - startup_script
    # - dac_config
    # - ip
    # - udp
    # - frequency_scale_factor
    # - amplitude_scale_factor
    # - phase_scale_factor
    # - num_instances
    # - channels_per_instance
    def init(self):


        self.logger = logging.getLogger("bluebox")
        self.logger.setLevel(logging.DEBUG)

        # Prevent duplicate handlers if this code runs more than once
        if not self.logger.handlers:
            self.logger.addHandler(RichStreamHandler())

        # Now use it in your code
        self.logger.error("Worker started")

        sys.path.append(os.path.join(os.path.dirname(__file__), self.build_path))
        from fvseei.lbus_access import lbus_access
        from fvseei.lbus_csr import Csr_Lbus
        from fvseei.multi_tone_controller import MultiToneController
        print(inspect.getfile(MultiToneController))
        print(inspect.getfile(lbus_access))
        print(inspect.getfile(Csr_Lbus))
        #print(fvseei.multi_tone_controller.__file__)


        cmd1 = self.vivado_path
        cmd2 = r".venv\Scripts\activate.bat" #activate virtual environment
        cmd3 = "python " + self.startup_script
        cmd4 = self.dac_config
        os.system(cmd1+";"+cmd2+";"+cmd3+";"+cmd4)

        chip = lbus_access(self.ip, timeout=1.02, port=self.udp, allow_burst=False)
        self.csr = Csr_Lbus(chip)

        self.tone_controllers = []
        for i in range(self.num_instances):
            controller = MultiToneController(self.csr, dds_index=i)
            self.tone_controllers.append(controller)
            self.logger.info(f"Initialized MultiToneController for DDS instance {i}")

    def write_register(self, instance, channel, value, csr, register_type):
        method_name = f"ddsctrl{instance}_{register_type}_{channel}_write"
        method = getattr(csr, method_name, None)
        if method:
            method(value)
        else:
            print(f"Method {method_name} not found.")

    def transition_to_buffered(self,device_name,h5file,initial_values,fresh):
        self.logger.info("transition_to_buffered START at time: " + str(datetime.datetime.now()))
        from fvseei.move_tone_sequencer_example import (reset_synthesizer, clear_sequencer_memory, write_sequencer_command, debug_sequencer_state)


        self.initial_values = initial_values
        self.final_values = {}

        
        # reset_synthesizer(self.csr, active_channels={0: list(range(9))})
        # clear_sequencer_memory(self.csr, num_slots=64)
        with h5py.File(h5file, 'r+') as hdf5_file:
            group = hdf5_file['/devices/'+device_name]

            initial_tones = {}
            active_channels = {}
            ramps = {}

            for dset in group.values(): #enumerate these two loops to then pass the correct channel and instance number i and channel number j into write_register
                
                core_test = [f'core{ell}' in dset.name for ell in range(4)]
                i = core_test.index(True)

                self.logger.info(f"processing {dset.name} {i}")


                if 'initials' in dset.name:
                    active_channels[i] = []
                    initial_tones[i] = []

                    for j,outputs in enumerate(dset):
                        gate_ij, freq_ij, amp_ij, phase_ij = outputs

                        if gate_ij == 1:
                            active_channels[i].append(j)
                            initial_tones[i].append((j, freq_ij, amp_ij, phase_ij))


                        self.final_values[f'freq_{i}_{j}']  = freq_ij
                        self.final_values[f'amp_{i}_{j}']   = amp_ij
                        self.final_values[f'phase_{i}_{j}'] = phase_ij
                        self.final_values[f'gate_{i}_{j}']  = gate_ij

                elif 'ramps' in dset.name:
                    ramps[i] = []

                    for outputs in dset:
                        channel, delay, duration, target_freq = outputs

                        ramps[i].append((channel, delay, duration, target_freq))
                else:
                    raise AttributeError("dataset found that does not contain \'initials\' or \'ramps\'")
            
        reset_synthesizer(self.csr, active_channels=active_channels)

        self.logger.info(f"active channels: {active_channels}")


        for i in ramps.keys():
            initial_list = initial_tones[i]
            ramp_list = ramps[i]
            controller_i = self.tone_controllers[i]

            controller_i.reset()

            for (j, freq_ij, amp_ij, phase_ij) in initial_list:
                controller_i.set_tone_lsb(
                    channel=j,
                    freq_lsb=int(np.round(freq_ij * one_sixth_MHz_lsb).astype(int)),
                    amplitude_lsb=amp_ij,
                    phase_lsb=phase_ij
                )


            for (j, delay, duration, target_freq) in ramp_list:
                controller_i.move_tone(
                    channel=j,
                    delay=delay,
                    duration=duration,
                    target_freq=int(np.round(target_freq * one_sixth_MHz_lsb).astype(int))
                )

                self.final_values[f'freq_{i}_{j}']= target_freq

            if len(ramp_list) > 0:
                print(f"Core {i} has moves:")
                print(controller_i.moves)

                debug_sequencer_state(self.csr)
                controller_i._build_sequencer_commands()
                controller._configure_presets()

                # Clear sequencer memory
                print("\n=== Programming sequencer ===")
                clear_sequencer_memory(self.csr, num_slots=max(16, len(controller.seq_commands) + 1))

                # Write commands
                for slot, (time_sec, cmd) in enumerate(controller.seq_commands):
                    write_sequencer_command(self.csr, slot, time_sec, cmd)
                    print(f"  Slot {slot}: t={time_sec:.1f}s")

                # Calculate total duration (last command + max ramp duration + margin)
                if controller.seq_commands:
                    last_time = controller.seq_commands[-1][0]
                    max_duration = max(duration for _, _, _, duration, _ in controller.moves)
                    total_duration = last_time + max_duration + 1.0
                else:
                    total_duration = 2.0

                # Set sequencer stopafter
                stopafter_cycles = int(total_duration * 153.6e6)
                self.csr.seq_stopafter_write(stopafter_cycles)
                print(f"\nTotal duration: {total_duration:.1f}s ({stopafter_cycles} cycles)")

                self.csr.status_clear_counters_write(1)
                time.sleep(0.01)


            getattr(self.csr, f"{controller_i.prefix}_syncddsstatic_write")(1)

        method = getattr(self.csr, "dac_global_clr_write", None)
        method(0)  
    
        # read_all_tones(self.csr)
        self.logger.info("transition_to_buffered END at time: " + str(datetime.datetime.now()))
        return self.final_values

    def check_remote_values(self):
        results = {}
        print("Checking Remote Values at time: " + str(datetime.datetime.now()))

        for i in np.arange(4):
            for j in np.arange(4):
                results[f'dds{i}_{j}'] = {}

                method = getattr(self.csr, f"ddsctrl{i}_ftw_{j}_read", None)
                freq_lsb = method() #in lsb
                method = getattr(self.csr, f"ddsctrl{i}_atw_{j}_read", None)
                amp_lsb = method()
                method = getattr(self.csr, f"ddsctrl{i}_ptw_{j}_read", None)
                phase_lsb = method()
                method = getattr(self.csr, f"ddsctrl{i}_clr_{j}_read", None)
                gate = 1-method()
                
                print(f"dds{i}_{j}: {freq_lsb}, {amp_lsb}, {phase_lsb}, {gate}")
                results[f'dds{i}_{j}']['freq'] = freq_lsb // one_sixth_MHz_lsb
                results[f'dds{i}_{j}']['amp'] = amp_lsb
                results[f'dds{i}_{j}']['phase'] = phase_lsb
                results[f'dds{i}_{j}']['gate'] = gate
        
        print("Got Remote Values at time: " + str(datetime.datetime.now()))
        return {}

    def program_manual(self,front_panel_values): #must modify for more channels and add for loops to loop through front_panel_values, don't know yet if we're gonna have 64*4 separate DDS widgets or how to display them
        self.logger.info("Programming Values Manually at time: " + str(datetime.datetime.now()))

        method = getattr(self.csr, "dac_global_clr_write", None)
        method(1)

        for i in np.arange(4):
            for j in np.arange(4):
                    if f'dds{i}_{j}' not in front_panel_values:
                        continue
                    freq  = front_panel_values[f'dds{i}_{j}']['freq'] * one_sixth_MHz_lsb
                    amp   = front_panel_values[f'dds{i}_{j}']['amp']
                    phase = front_panel_values[f'dds{i}_{j}']['phase']
                    gate  = front_panel_values[f'dds{i}_{j}']['gate']
                    self.logger.info(f"{i}_{j}: {freq}, {amp}, {phase}, {gate}")
                    
                    method = getattr(self.csr, f"ddsctrl{i}_ftw_{j}_write", None)
                    method(freq)
                    method = getattr(self.csr, f"ddsctrl{i}_atw_{j}_write", None)
                    method(amp)
                    method = getattr(self.csr, f"ddsctrl{i}_ptw_{j}_write", None)
                    method(phase)
                    method = getattr(self.csr, f"ddsctrl{i}_clr_{j}_write", None)
                    method(1 - gate)

            method = getattr(self.csr, f"ddsctrl{i}_syncddsstatic_write", None)
            method(1)
            self.logger.info(f"ddsctrl{i}_syncddsstatic_write(1)")

        method = getattr(self.csr, "dac_global_clr_write", None)
        method(0)  
        self.logger.info(f"dac_global_clr_write(0)")

        self.logger.info("Done Programming values Manually at time: " + str(datetime.datetime.now()))

        return self.check_remote_values()
    

    def abort_transition_to_buffered(self):
        return self.transition_to_manual(True)
        
    def abort_buffered(self):
        return self.transition_to_manual(True)
    
    def shutdown(self):
        pass
    
    def transition_to_manual(self,abort = False):
        self.logger.info("Starting Transition to Manual at time: " + str(datetime.datetime.now()))
        if abort:
            # If we're aborting the run, reset to original value
            self.program_manual(self.initial_values)
        # If we're not aborting the run, stick with buffered value. Nothing to do really!
        # return the current values in the device
        self.logger.info("Ending Transition to Manual at time: " + str(datetime.datetime.now()))
        return True