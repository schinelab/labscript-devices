import numpy as np
from labscript import Device, StaticDDS, StaticAnalogQuantity, StaticDigitalOut, set_passed_properties
import labscript_utils.h5_lock
import h5py
from labscriptlib.Yb171_MOT_Tweezer_trap.custom_unit_conversion_classes import *

Hz_per_lsb = 614.4 * 10**6 / (2**32)
lsb_per_MHz = np.round(1e6 / Hz_per_lsb).astype(int)
one_sixth_MHz_lsb = lsb_per_MHz // 6

class BlueBoxDDS(StaticDDS):

    description = 'Multichromatic DDS'
    allowed_children = [StaticAnalogQuantity,StaticDigitalOut]
    generation = 2
    
    @set_passed_properties()    
    def __init__(self, 
        name, 
        parent_device, 
        connection, 
        freq_limits = (0, 3686), 
        freq_conv_class = blueboxFreqUnitConversion,
        freq_conv_params = {},
        amp_limits = (0, 2**16-1), 
        amp_conv_class = blueboxAmpUnitConversion,
        amp_conv_params = {}, 
        phase_limits = (0, 2**18-1), 
        phase_conv_class = blueboxPhaseUnitConversion,
        phase_conv_params = {}
    ):
        Device.__init__(self,name,parent_device,connection)

        self.starting_gate  = []
        self.starting_freq  = []
        self.starting_amp   = []
        self.starting_phase = []
        self.ramps          = []
    

        for _ in range(self.parent_device.num_instances):


            self.starting_gate.append([])
            self.starting_freq.append([])
            self.starting_amp.append([])
            self.starting_phase.append([])
            self.ramps.append([])
        
        for i in range(self.parent_device.num_instances):
            for j in range(self.parent_device.channels_per_instance[i]):
                output_name = f'inst{i}_chan{j}'

                gate  =     StaticDigitalOut(self.name+'_'+output_name+'_gate'  , self, output_name+'_gate')
                freq  = StaticAnalogQuantity(self.name+'_'+output_name+'_freq'  , self, output_name+'_freq' , freq_limits , freq_conv_class,  freq_conv_params, default_value=0)
                amp   = StaticAnalogQuantity(self.name+'_'+output_name+'_amp'   , self, output_name+'_amp'  , amp_limits  , amp_conv_class,   amp_conv_params, default_value=0)
                phase = StaticAnalogQuantity(self.name+'_'+output_name+'_phase' , self, output_name+'_phase', phase_limits, phase_conv_class, phase_conv_params, default_value=0)

                self.starting_gate[i].append(gate)
                self.starting_freq[i].append(freq)
                self.starting_amp[i].append(amp)
                self.starting_phase[i].append(phase)


    def set_tone_lsb(self, instance, channel, freq_lsb, amp_lsb, phase_lsb, units = (None, None, None)):
        unit_f, unit_a, unit_p = units
        self.enable(instance, channel)
        self.set_freq_lsb(instance, channel, freq_lsb, units=unit_f)
        self.set_amp_lsb(instance, channel, amp_lsb, units=unit_a)
        self.set_phase_lsb(instance, channel, phase_lsb, units=unit_p)

    def set_tones_lsb(self, instance, channels, freqs_lsb, amps_lsb, phases_lsb, units = (None, None, None)):
        for i in len(channels):
            self.set_tone_lsb(instance, channels[i], freqs_lsb[i], amps_lsb[i], phases_lsb[i], units=units)

    def set_freq_lsb(self, instance, channel, value, units=None): #instance and channel being zero-indexed
        self.starting_freq[instance][channel].constant(value, units)
        
    def set_amp_lsb(self, instance, channel, value, units=None): #instance and channel being zero-indexed
        self.starting_amp[instance][channel].constant(value, units)

    def set_phase_lsb(self, instance, channel, value, units=None): #instance and channel being zero-indexed
        self.starting_phase[instance][channel].constant(value, units)
            
    def enable(self, instance, channel):
        self.starting_gate[instance][channel].go_high()
                            
    def disable(self, instance, channel):
        self.starting_gate[instance][channel].go_low()

    def ramp(self, instance, channel, duration, delay, target_freq_lsb): #duration and delay after trigger (can be very small but cannot be zero) in sec, target_freq in MHz
        self.ramps[instance].append((channel, delay, duration, target_freq_lsb))

    
class BlueBox(Device):
    # A custom multichromatic DDS used to generate optical tweezers
    description = 'Multichromatic DDS Box'
    allowed_children = [BlueBoxDDS]
    generation = 0

    @set_passed_properties(
        property_names={
            "connection_table_properties": [
                "build_path",
                "vivado_path",
                "startup_script",
                "dac_config",
                "ip",
                "udp"
            ],
            "device_properties": [
                "num_instances",
                "channels_per_instance"
            ]            
        }
    )
    def __init__(
        self,
        name,
        com_port,
        build_path,
        vivado_path,
        startup_script,
        dac_config,
        ip,
        udp
    ):
        Device.__init__(self, name, None, None)
        self.BLACS_connection = com_port
        self.build_path = build_path
        self.vivado_path = vivado_path
        self.startup_script = startup_script
        self.dac_config = dac_config
        self.ip = ip
        self.udp = udp
        self.num_instances = 4
        self.channels_per_instance = [64,64,32,32]
        self.channel_outputs = []


    def generate_code(self, hdf5_file):
        
        group = self.init_device_group(hdf5_file)

        initial_dtypes = [
            ('gate', int),
            ('freq', int),
            ('amp', int),
            ('phase', int)
        ]
        
        ramp_dtypes = [
            ('channel', int),
            ('delay', float),
            ('duration', float),
            ('target_freq', int)
        ]

        multidds = self.child_devices[0]

        starting_lists = [multidds.starting_gate, multidds.starting_freq, multidds.starting_amp, multidds.starting_phase]

        for i in range(self.num_instances):
            self.channel_outputs = []

            for j in range(self.channels_per_instance[i]):

                for sl in starting_lists:
                    _ = sl[i][j].get_change_times()
                    sl[i][j].make_timeseries([])
                    sl[i][j].expand_timeseries([])

                gate  = multidds.starting_gate[i][j]
                freq  = multidds.starting_freq[i][j]
                amp   = multidds.starting_amp[i][j]
                phase = multidds.starting_phase[i][j]

                #upon full completion of the inner loop, will be a fully populated array of channel outputs for a single instance, then gets erased and moves on to the next instance
                self.channel_outputs.append(
                    (
                        gate.raw_output[0],
                        freq.raw_output[0],
                        amp.raw_output[0],
                        phase.raw_output[0]
                    )
                )


            data = np.array(self.channel_outputs, dtype=initial_dtypes)

            group.create_dataset(f"core{i}_initials", data=data)


            data = np.array(multidds.ramps[i], dtype=ramp_dtypes)

            group.create_dataset(f"core{i}_ramps", data=data)

        #raw_output of a StaticAnalogQuantity/StaticDigitalOut is set to an array with a single element equal to its constant output value



