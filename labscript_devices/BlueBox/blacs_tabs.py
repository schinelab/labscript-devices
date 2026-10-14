from blacs.tab_base_classes import define_state, MODE_MANUAL
from blacs.device_base_class import DeviceTab
from labscript_devices import BLACS_tab
import h5py
import numpy as np
import labscript_utils.properties

Hz_per_lsb = 614.4 * 10**6 / (2**32)
lsb_per_MHz = np.round(1e6 / Hz_per_lsb).astype(int)
one_sixth_MHz_lsb = lsb_per_MHz // 6

@BLACS_tab
class BlueBoxTab(DeviceTab):

    def initialise_GUI(self):
        
        single_dds_channel = {'freq':{'base_unit': 'sixthMHz',
                                     'min':         0,
                                     'max':         3686,
                                     'step':        1,
                                     'decimals':    0},
                             'amp'  :{'base_unit': 'lsb',
                                     'min':         0,
                                     'max':         2**16-1,
                                     'step':        2**10,
                                     'decimals':    0},
                             'phase':{'base_unit': 'lsb',
                                     'min':         0,
                                     'max':         2**18-1,
                                     'step':        (2**18-1)/360,
                                     'decimals':    0},
                             'gate' :{}
                                 }
        dds_prop = {}

        for i in np.arange(4):
            for j in np.arange(4):
                dds_prop[f'dds{i}_{j}'] = single_dds_channel.copy()

        # self.create_dds_outputs(dds_prop)        
        # # Create widgets for output objects
        # dds_widgets,_,_ = self.auto_create_widgets()
        # # and auto place the widgets in the UI
        # self.auto_place_widgets(("DDS Outputs",dds_widgets))

        self.supports_remote_value_check(True)
        self.supports_smart_programming(False) 

    def initialise_workers(self):
        table = self.settings['connection_table']
        connection_table_properties = table.find_by_name(self.device_name).properties
        with h5py.File(table.filepath, 'r') as f:
            device_properties = labscript_utils.properties.get(
                f, self.device_name, "device_properties"
            )

        worker_initialisation_kwargs = {
            'build_path': connection_table_properties['build_path'],
            'vivado_path': connection_table_properties['vivado_path'],
            'startup_script': connection_table_properties['startup_script'],
            'dac_config': connection_table_properties['dac_config'],
            'ip': connection_table_properties['ip'],
            'udp': connection_table_properties['udp'],
            'num_instances': device_properties['num_instances'],
            'channels_per_instance': device_properties['channels_per_instance']
        }
        self.create_worker(
            "main_worker", "labscript_devices.BlueBox.blacs_workers.BlueBoxWorker", worker_initialisation_kwargs
        )
        self.primary_worker = "main_worker"

# Override standard check remote values to bypass value changes warnings and always use the remote values

    @define_state(MODE_MANUAL,True)
    def check_remote_values(self):
        prev_rv = self._last_remote_values
        self._last_remote_values = yield(self.queue_work(self._primary_worker,'check_remote_values'))

        if self._last_remote_values != prev_rv:
            yield(self.queue_work(self._primary_worker,'program_manual', self._last_remote_values))

            self._last_remote_values = yield(self.queue_work(self._primary_worker,'check_remote_values'))

            self.restore_save_data(self._last_remote_values)

            self._last_programmed_values = self._last_remote_values