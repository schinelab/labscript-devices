from __future__ import division, unicode_literals, print_function, absolute_import
from labscript_devices import runviewer_parser, BLACS_tab
from labscript_devices import AD9910
from labscript_devices.AD9910 import  AD9910
from labscript import IntermediateDevice, Device, config, LabscriptError, set_passed_properties
from labscript_utils.unitconversions import NovaTechDDS9mFreqConversion, NovaTechDDS9mAmpConversion, FreqConversion

import numpy as np
import labscript_utils.h5_lock, h5py
import labscript_utils.properties

bauds = {9600: b'Kb 78',
         19200: b'Kb 3c',
         38400: b'Kb 1e',
         57600: b'Kb 14',
         115200: b'Kb 0a'}

class lsduino(IntermediateDevice):
    description = 'Labscript controlled microcontroller'
    allowed_children = [AD9910]
    clock_limit = 9990 # I need to find out what this would be for the Arduino

    @set_passed_properties(
        property_names = {'connection_table_properties': ['com_port', 'baud_rate', 'default_baud_rate', 'update_mode',
        'synchronous_first_line_repeat','ndev']}#I'm not sure which of these I'll keep
        )
    def __init__(self, name, parent_device,
                 com_port = '', baud_rate=9600, default_baud_rate = None, update_mode='synchronous',
                 synchronous_first_line_repeat = True, ndev = 2, **kwargs):

        IntermediateDevice.__init__(self, name, parent_device, **kwargs)
        self.BLACS_connection = '%s,%s'%(com_port, str(baud_rate))

        if not update_mode in ['synchronous', 'asyncronous']:
            raise LabscriptError('update_mode must be \'synchronous\' or \'asynchronous\'')

        if not baud_rate in bauds:
            raise LabscriptError('baud_rate must be one of {0}'.format(list(bauds)))

        if not default_baud_rate in bauds and default_baud_rate is not None:
            raise LabscriptError('default_baud_rate must be one of {0} or None (to indicate no default)'.format(list(bauds)))

        self.ndev = ndev
        
        self.update_mode = update_mode
        self.synchronous_first_line_repeat = synchronous_first_line_repeat

    def add_device(self, device):
        Device.add_device(self, device)
        device.frequency.default_value = 80000000
        device.rampstartf.default_value = 20000000
        device.rampendf.default_value = 20000000
        device.rampdur.default_value = 1
        device.rampon.default_value = 0


    def get_default_unit_conversion_classes(self, device):
        return FreqConversion #Check up on this and make these files?

    def quantise_freq(self, data, device):
        if not isinstance(data, np.ndarray):
            data = np.array(data)
        if np.any(data > 400e6) or np.any(data < 0.1):
            raise LabscriptError('%s %s'%(device.description, device.name) + 'can only have frequencies between 0.1Hz and 4000MHz')
        data = np.array(data,dtype=np.float32)
        return data

    def quantise_dur(self, data, device):
        if not isinstance(data, np.ndarray):
            data = np.array(data)
        if np.any(data < 0.001):
            raise LabscriptError('%s %s'%(device.description, device.name) + 'I decided that the duration must be at least 1 ms')
        data = np.array(data,dtype=np.float32)
        return data

    def quantise_rampon(self, data, device):
        if not isinstance(data, np.ndarray):
            data = np.array(data)
        if np.any(data > 1) or np.any(data < 0):
            raise LabscriptError('%s %s'%(device.description, device.name) + 'supposed to be binary')
        data = np.array(data,dtype=np.uint8)
        return data

    def generate_code(self, hdf5_file):
        DDSs = {}
        for output in self.child_devices:
            ## Check that the instructions will fit into RAM:
            #if isinstance(output, DDSAD9954) and len(output.frequency.raw_output) > 16384 - 2: # -2 to include space for dummy instructions, not sure if this holds for arduinos
            #    raise LabscriptError('%s can only support 16383 instructions. '%self.name +
            #                         'Please decrease the sample rates of devices on the same clock, ' +
            #                         'or connect %s to a different pseudoclock.'%self.name)
            try:
                prefix, channel = output.connection.split()
                channel = int(channel)
            except:
                raise LabscriptError('%s %s has invalid connection string: \'%s\'. '%(output.description,output.name,str(output.connection)) +
                                     'Format must be \'channel n\' with n from 0 to 3.')
            DDSs[channel] = output
        for connection in DDSs:
            if connection in range(len(self.child_devices)):
                # Dynamic DDS
                dds = DDSs[connection]
                dds.frequency.raw_output = self.quantise_freq(dds.frequency.raw_output, dds)
                dds.rampstartf.raw_output = self.quantise_freq(dds.rampstartf.raw_output, dds)
                dds.rampendf.raw_output = self.quantise_freq(dds.rampendf.raw_output, dds)
                dds.rampdur.raw_output = self.quantise_dur(dds.rampdur.raw_output, dds)
                dds.rampon.raw_output = self.quantise_rampon(dds.rampon.raw_output, dds)
            # elif connection in range(2,4):
                # # StaticDDS:
                # dds = DDSs[connection]
                # dds.frequency.raw_output, dds.frequency.scale_factor = self.quantise_freq(dds.frequency.static_value, dds)
                # dds.phase.raw_output, dds.phase.scale_factor = self.quantise_phase(dds.phase.static_value, dds)
                # dds.amplitude.raw_output, dds.amplitude.scale_factor = self.quantise_amp(dds.amplitude.static_value, dds)
            else:
                raise LabscriptError('%s %s has invalid connection string: \'%s\'. '%(dds.description,dds.name,str(dds.connection)) +
                                     'Format must be \'channel n\' with n from 0 to 3.')

        dtypes = [('freq%d'%i,np.float32) for i in range(len(self.child_devices))] + \
                 [('rampstartf%d'%i,np.float32) for i in range(len(self.child_devices))] + \
                 [('rampendf%d'%i,np.float32) for i in range(len(self.child_devices))] + \
                 [('rampdur%d'%i,np.float32) for i in range(len(self.child_devices))] + \
                 [('rampon%d'%i,np.uint8) for i in range(len(self.child_devices))]

        clockline = self.parent_clock_line
        pseudoclock = clockline.parent_device
        times = pseudoclock.times[clockline]

        out_table = np.zeros(len(times),dtype=dtypes)

        for connection in range(len(self.child_devices)):
            if not connection in DDSs:
                continue
            dds = DDSs[connection]
            # The last two instructions are left blank, for BLACS
            # to fill in at program time.
            out_table['freq%d'%connection].fill(1)
            out_table['freq%d'%connection][:] = dds.frequency.raw_output
            out_table['rampstartf%d'%connection][:] = dds.rampstartf.raw_output
            out_table['rampendf%d'%connection][:] = dds.rampendf.raw_output
            out_table['rampdur%d'%connection][:] = dds.rampdur.raw_output
            out_table['rampon%d'%connection][:] = dds.rampon.raw_output

        if self.update_mode == 'asynchronous' or self.synchronous_first_line_repeat:
            # Duplicate the first line of the table. Otherwise, we are one step
            # ahead in the table from the start of a run. In asynchronous
            # updating mode, this is necessary since the first line of the
            # table is already being output before the first trigger from
            # the master clock. When using a simple delay line for synchronous
            # output, this also seems to be required, in which case
            # synchronous_first_line_repeat should be set to True.
            # However, when a tristate driver is used as described at
            # http://labscriptsuite.org/blog/implementation-of-the-novatech-dds9m/
            # then is is not neccesary to duplicate the first line. Use of a
            # tristate driver in this way is the correct way to use
            # the novatech DDS, as per its instruction manual, and so is likely
            # to be the most reliable. However, through trial and error we've
            # determined that duplicating the first line like this gives correct
            # output in asynchronous mode and in synchronous mode when using a
            # simple delay line, at least for the specific device we tested.
            # Your milage may vary.
            out_table = np.concatenate([out_table[0:1], out_table])

        grp = self.init_device_group(hdf5_file)
        grp.create_dataset('TABLE_DATA',compression=config.compression,data=out_table)
        self.set_property('frequency_scale_factor', 1, location='device_properties')



import time

from blacs.tab_base_classes import Worker, define_state
from blacs.tab_base_classes import MODE_MANUAL, MODE_TRANSITION_TO_BUFFERED, MODE_TRANSITION_TO_MANUAL, MODE_BUFFERED
from blacs.device_base_class import DeviceTab

@BLACS_tab
class lsduinoTab(DeviceTab):
    def initialise_GUI(self):

        connection_object = self.settings['connection_table'].find_by_name(self.device_name)
        connection_table_properties = connection_object.properties

        self.com_port = connection_table_properties.get('com_port', None)
        self.baud_rate = connection_table_properties.get('baud_rate', None)
        self.default_baud_rate = connection_table_properties.get('default_baud_rate', None)
        self.update_mode = connection_table_properties.get('update_mode', 'synchronous')
        
        self.ndev = connection_table_properties.get('ndev', 2)

        # Capabilities
        self.base_units =    {'freq':'Hz', 'rampstartf': 'Hz', 'rampendf': 'Hz', 'rampdur': 's', 'rampon': ''}
        self.base_min =      {'freq':0.0, 'rampstartf': 0.0, 'rampendf': 0.0, 'rampdur': 0, 'rampon': 0}
        self.base_max =      {'freq':400.0*10.0**6, 'rampstartf': 400.0*10.0**6, 'rampendf': 400.0*10.0**6, 'rampdur': 1*10**5, 'rampon': 1}
        self.base_step =     {'freq':0.1*10**6, 'rampstartf':0.1**6, 'rampendf':0.1**6, 'rampdur': 1, 'rampon': 1}
        self.base_decimals = {'freq':1, 'rampstartf':1, 'rampendf':1, 'rampdur': 1, 'rampon': 1}
        
        # Create DDS Output objects
        dds_prop = {}
        for i in range(self.ndev): 
            dds_prop['channel %d'%i] = {}
            for subchnl in ['freq', 'rampstartf', 'rampendf', 'rampdur', 'rampon']:
                dds_prop['channel %d'%i][subchnl] = {'base_unit':self.base_units[subchnl],
                                                     'min':self.base_min[subchnl],
                                                     'max':self.base_max[subchnl],
                                                     'step':self.base_step[subchnl],
                                                     'decimals':self.base_decimals[subchnl]
                                                    }
        #print(len(dds_prop))
        # Create the output objects
        self.create_dds_outputs(dds_prop)
        #self.create_analog_outputs(ao_prop)
        #self.create_digital_outputs(do_prop)
        # Create widgets for output objects
        dds_widgets,ao_widgets,do_widgets = self.auto_create_widgets()
        # and auto place the widgets in the UI
        self.auto_place_widgets(("DDS Outputs",dds_widgets))
        #self.auto_place_widgets(("Analog Outs",ao_widgets))
        #self.auto_place_widgets(("Digital Outs",do_widgets))

        

        # Backward compat:
        blacs_connection =  str(connection_object.BLACS_connection)
        if ',' in blacs_connection:
            com_port, baud_rate = blacs_connection.split(',')
            if self.com_port is None:
                self.com_port = com_port
            if self.baud_rate is None:
                self.baud_rate = int(baud_rate)
        else:
            self.com_port = blacs_connection
            self.baud_rate = 115200



        # Create and set the primary worker
        self.create_worker("main_worker",lsduinoWorker,{'com_port':self.com_port,
                                                              'baud_rate': self.baud_rate,
                                                              'default_baud_rate': self.default_baud_rate,
                                                              'update_mode': self.update_mode,
                                                              'ndev':self.ndev})
        self.primary_worker = "main_worker"

        # Set the capabilities of this device
        self.supports_remote_value_check(False)
        self.supports_smart_programming(False)


class lsduinoWorker(Worker):
    def init(self):
        global serial; import serial
        global socket; import socket
        global h5py; import labscript_utils.h5_lock, h5py
        import logging
        self.smart_cache = {'STATIC_DATA': None, 'TABLE_DATA': ''}
        self.logger = logging.getLogger('BLACS.%s.state_queue'%('lsduino'))
        
        if self.default_baud_rate is not None:
            initial_baud_rate = self.default_baud_rate
        else:
            initial_baud_rate = self.baud_rate
        self.initial_baud_rate = initial_baud_rate
        
        self.connection = serial.Serial(
            self.com_port, baudrate=initial_baud_rate, timeout=0.1
        )

    def program_manual(self,front_panel_values):
        # TODO: Optimise this so that only items that have changed are reprogrammed by storing the last programmed values
        # For each DDS channel,
        for i in range(self.ndev):
            # and for each subchnl in the DDS,
            #print(b'@ %d\r\n'%i)
            self.connection.write(b'@ %d\r\n'%i)
            print(b'@ %d\r\n'%i)
            #if front_panel_values['channel %d'%i]['rampon'] == 1:
            #    rampstartf = front_panel_values['channel %d'%i]['rampstartf']
            #    rampendf = front_panel_values['channel %d'%i]['rampendf']
            #    rampdur = front_panel_values['channel %d'%i]['rampdur']
            #    self.connection.write(b'r %f %f %f\r\n'%(rampstartf, rampendf, rampdur))
            #else:
            for subchnl in ['freq']:
                    # Program the sub channel
                self.program_static(i,subchnl,front_panel_values['channel %d'%i][subchnl])
        self.connection.write(b'$\r\n')
        print(b'$\r\n')

    def program_static(self,channel,type,value):
        #print('start program_static()')
        if type == 'freq':
            command = b'f %f\r\n'%value
        else:
            raise TypeError(type)
        #print(command)
        self.connection.write(command)
        print(command)
        #print('done')
        # Now that a static update has been done, we'd better invalidate the saved STATIC_DATA:
        self.smart_cache['STATIC_DATA'] = None
        #print('end program_static()')
        #print('')

    def transition_to_buffered(self,device_name,h5file,initial_values,fresh):
        self.connection.close()
        self.connection = serial.Serial(
            self.com_port, baudrate=self.initial_baud_rate, timeout=0.1
        )
        print('start transition_to_buffered()')
        # Pretty please reset your memory pointer to zero:

        # Store the initial values in case we have to abort and restore them:
        # Store the final values to for use during transition_to_static:
        self.final_values = {}
        table_data = None
        
        # Jeff edit to fix error in blacs tab when running experiments from runmanager
        # with h5py.File(h5file) as hdf5_file:
        print('opening h5 file')
        with h5py.File(h5file,mode='r') as hdf5_file:
            group = hdf5_file['/devices/'+device_name]
            # Now program the buffered outputs:
            if 'TABLE_DATA' in group:
                table_data = group['TABLE_DATA'][:]
        print('table data loaded from file')

        # Now program the buffered outputs:

        if table_data is not None:
            data = table_data
            print('sending data tables')
            for ddsno in range(self.ndev):
                print('sending data table {:f}'.format(ddsno))
                commandString = b'@ %d\r\n'%ddsno
                self.connection.write(commandString)
                print(commandString)
                for i, line in enumerate(data):
                    print('sending line {:f}'.format(i))
                    oldtable = self.smart_cache['TABLE_DATA']
                    if fresh or i >= len(oldtable) or (line['freq%d'%ddsno], line['rampstartf%d'%ddsno], line['rampendf%d'%ddsno],
                                                       line['rampdur%d'%ddsno], line['rampon%d'%ddsno]) != (oldtable[i]['freq%d'%ddsno],
                                                       oldtable[i]['rampstartf%d'%ddsno], oldtable[i]['rampendf%d'%ddsno],
                                                       oldtable[i]['rampdur%d'%ddsno], oldtable[i]['rampon%d'%ddsno]):
                        if line['rampon%d'%ddsno] == 1:
                            commandString = b'r %f %f %f\r\n'%(line['rampstartf%d'%ddsno], line['rampendf%d'%ddsno], line['rampdur%d'%ddsno])
                            self.connection.write(commandString)
                            print(commandString)
                            # self.logger.info("Programming ramp: " + str(commandString))
                        else:
                            commandString = b'f %f\r\n'%line['freq%d'%ddsno]
                            self.connection.write(commandString)
                            print(commandString)
            
            # Store the table for future smart programming comparisons:
            #print('set smart cache thing')
            try:
                self.smart_cache['TABLE_DATA'][:len(data)] = data
                self.logger.debug('Stored new table as subset of old table')
            except: # new table is longer than old table
                self.smart_cache['TABLE_DATA'] = data
                self.logger.debug('New table is longer than old table and has replaced it.')

            #print('setting final values')
            # Get the final values of table mode so that the GUI can
            # reflect them after the run:
            for ddsno in range(self.ndev):
                self.final_values['channel %d'%ddsno] = {}
                self.final_values['channel %d'%ddsno]['freq'] = data[-1]['freq%d'%ddsno]

            #print('reading from arduino?')
            # self.connection.readline()
            if self.update_mode == 'synchronous':
                # Transition to hardware synchronous updates:
                self.connection.write(b'$\r\n')
                print(b'$\r\n')
                # self.connection.readline()
                # We are now waiting for a rising edge to trigger the output
                # of the second table pair (first of the experiment)
            elif self.update_mode == 'asynchronous':
                # Output will now be updated on falling edges.
                pass
            else:
                raise ValueError('invalid update mode %s'%str(self.update_mode))

        serial_lines = self.connection.readlines()
        print("----------------Transition to Buffered Serial Responses----------------")
        for s in serial_lines:
            print(s.decode('ascii').strip())
        print("-----------------------------------------------------------------------")

        #print('end transition_to_buffered()')
        #print('')
        return self.final_values

    def abort_transition_to_buffered(self):
        return self.transition_to_manual(True)

    def abort_buffered(self):
        # TODO: untested
        return self.transition_to_manual(True)

    def transition_to_manual(self,abort = False):
        print('starting transition to manual')
        if abort:
            # If we're aborting the run, then we need to reset DDSs 2 and 3 to their initial values.
            # 0 and 1 will already be in their initial values. We also need to invalidate the smart
            # programming cache for them.
            #values = self.initial_values
            DDSs = []
            self.smart_cache['STATIC_DATA'] = None
        else:
            # If we're not aborting the run, then we need to set DDSs 0 and 1 to their final values.
            # 2 and 3 will already be in their final values.
            values = self.final_values
            DDSs = range(self.ndev)
        
        self.connection.write(b'?\r\n')

        # only program the channels that we need to
        for ddsnumber in DDSs:
            channel_values = values['channel %d'%ddsnumber]
            self.connection.write(b'@ %d\r\n'%ddsnumber)
            print(b'@ %d\r\n'%ddsnumber)
            for subchnl in ['freq']:
                self.program_static(ddsnumber,subchnl,channel_values[subchnl])
            self.connection.write(b'$\r\n')
            print(b'$\r\n')

        serial_lines = self.connection.readlines()
        print("-----------------Transition to Manual Serial Responses-----------------")
        for s in serial_lines:
            print(s.decode('ascii').strip())
        print("-----------------------------------------------------------------------")

        # return True to indicate we successfully transitioned back to manual mode
        return True

    def shutdown(self):

        # return to the default baud rate
        if self.default_baud_rate is not None:
            self.connection.write(b'%s\r\n' % bauds[self.default_baud_rate])
            print(b'%s\r\n' % bauds[self.default_baud_rate])
            time.sleep(0.1)
            self.connection.readlines()

        self.connection.close()



@runviewer_parser
class RunviewerClass(object):
    def __init__(self, path, device):
        self.path = path
        self.name = device.name
        self.device = device

    def get_traces(self, add_trace, clock=None):
        if clock is None:
            # we're the master pseudoclock, software triggered. So we don't have to worry about trigger delays, etc
            raise Exception('No clock passed to %s. The NovaTechDDS9M must be clocked by another device.'%self.name)

        times, clock_value = clock[0], clock[1]

        clock_indices = np.where((clock_value[1:]-clock_value[:-1])==1)[0]+1
        # If initial clock value is 1, then this counts as a rising edge (clock should be 0 before experiment)
        # but this is not picked up by the above code. So we insert it!
        if clock_value[0] == 1:
            clock_indices = np.insert(clock_indices, 0, 0)
        clock_ticks = times[clock_indices]

        # get the data out of the H5 file
        data = {}
        with h5py.File(self.path, 'r') as hdf5_file:
            if 'TABLE_DATA' in hdf5_file['devices/%s' % self.name]:
                table_data = hdf5_file['devices/%s/TABLE_DATA' % self.name][:]
                connection_table_properties = labscript_utils.properties.get(hdf5_file, self.name, 'connection_table_properties')
                update_mode = getattr(connection_table_properties, 'update_mode', 'synchronous')
                synchronous_first_line_repeat = getattr(connection_table_properties, 'synchronous_first_line_repeat', False)
                if update_mode == 'asynchronous' or synchronous_first_line_repeat:
                    table_data = table_data[1:]
                for i in range(len(self.child_devices)):
                    for sub_chnl in ['freq', 'rampstartf', 'rampendf', 'rampdur', 'rampon']:
                        data['channel %d_%s'%(i,sub_chnl)] = table_data['%s%d'%(sub_chnl,i)][:]


        for channel, channel_data in data.items():
            data[channel] = (clock_ticks, channel_data)

        for channel_name, channel in self.device.child_list.items():
            for subchnl_name, subchnl in channel.child_list.items():
                connection = '%s_%s'%(channel.parent_port, subchnl.parent_port)
                if connection in data:
                    add_trace(subchnl.name, data[connection], self.name, connection)

        return {}
