import sys
from labscript_utils import dedent
from labscript import TriggerableDevice, set_passed_properties, AnalogQuantity, DigitalQuantity
import numpy as np
import h5py

class OrcaQuest(TriggerableDevice):
    description = 'Hamamatsu OrcaQuest 2 Camera'
    allowed_children = [AnalogQuantity,DigitalQuantity]
    generation=2
    
    @set_passed_properties(
        property_names={
            "device_properties": [ #available property tables with possible values can be found in the SDK files, >dcamsdk4>doc>cameraproperties>propC15550-22UP_en
                "trigger_source",
                "attr_dict",
                "rearrange_active",
                "stop_acquisition_timeout" #How long, in s, to wait after the shot finishes for the acquisition of images to complete before giving up.         
            ],
        }
    )
    def __init__( #parameters with default values don't need to be specified in the connection table
        self,
        name,
        parent_device,
        connection,
        serial_number,
        trigger_source,
        attr_dict,
        rearrange_active=False,
        stop_acquisition_timeout=10.0,
        **kwargs
    ):

        self.parent_device = parent_device
        self.BLACS_connection = serial_number
        self.trigger_source = trigger_source
        self.stop_acquisition_timeout = stop_acquisition_timeout
        self.attr_dict = attr_dict
        self.rearrange_active = rearrange_active
        self.exposures = []
        self.shot_settings_changed = False
        self.shot_settings = {}
        self.sync_readout_num = -1

        TriggerableDevice.__init__(self, name, parent_device, connection, **kwargs)

    def expose(self, t, name, frametype='frame', exposure_time = 1e-3):
        """Request an exposure at the given time. A trigger will be produced by the
        parent trigger object, with duration trigger_duration, or if not specified, of
        self.trigger_duration. The frame should have a `name, and optionally a
        `frametype`, both strings. These determine where the image will be stored in the
        hdf5 file. `name` should be a description of the image being taken, such as
        "insitu_absorption" or "fluorescence" or similar. `frametype` is optional and is
        the type of frame being acquired, for imaging methods that involve multiple
        frames. For example an absorption image of atoms might have three frames:
        'probe', 'atoms' and 'background'. For this one might call expose three times
        with the same name, but three different frametypes.
        """
        #consecutive exposures should not be scheduled closer together than the trigger duration

        # Backward compatibility with code that calls expose with name as the first
        # argument and t as the second argument:
        if isinstance(t, str) and isinstance(name, (int, float)):
            msg = """expose() takes `t` as the first argument and `name` as the second
                argument, but was called with a string as the first argument and a
                number as the second. Swapping arguments for compatibility, but you are
                advised to modify your code to the correct argument order."""
            print(dedent(msg), file=sys.stderr)
            t, name = name, t
        if exposure_time <= 0:
            msg = "trigger_duration must be > 0, not %s" % str(trigger_duration)


            raise ValueError(msg)
        self.parent_device.go_high(t)
        self.parent_device.go_low(t + exposure_time)
        self.exposures.append((t, name, frametype, exposure_time))
        return 0   

    def clear_sensor(self, t, exposure_time=0.1e-3):
        if not exposure_time > 0:
            msg = "exposure_time must be > 0, not %s" % str(exposure_time)
            raise ValueError(msg)
        
        self.parent_device.go_high(t)
        self.parent_device.go_low(t + exposure_time)
        self.exposures.append((t, 'DISCARD', 'DISCARD', exposure_time))
        return 0

    def change_camera_settings(self, shot_settings):
        if shot_settings is None or not isinstance(shot_settings,dict):
            return 
        if self.shot_settings_changed:
            msg = f"Camera Settings can only be changed once per shot, please combine multiple calls into a single call"
            raise ValueError(msg)
        
        self.shot_settings_changed = True
        self.shot_settings = shot_settings

    def generate_code(self, hdf5_file):
        self.do_checks()

        group = self.init_device_group(hdf5_file)

        if self.sync_readout_num > 0:
            group.attrs.create('SYNCREADOUT_NUM', self.sync_readout_num)

            if len(self.exposures) != 1:
                raise ValueError("On Sync readout mode we can only trigger the camera once")
            exposure = self.exposures[0]
            self.exposures = []    
            for i in np.arange(self.sync_readout_num):
                self.exposures.append((exposure[0], exposure[1] + f" {i:04d}", exposure[2], exposure[3]))

        vlenstr = h5py.special_dtype(vlen=str)
        table_dtypes = [
            ('t', float),
            ('name', vlenstr),
            ('frametype', vlenstr),
            ('exposure_time', float),
        ]
        data = np.array(self.exposures, dtype=table_dtypes)

        if self.exposures:
            group.create_dataset('EXPOSURES', data=data)


        if self.shot_settings_changed:
            new_settings = group.create_group("NEW_SHOT_SETTINGS")
            group.create_group("OLD_SHOT_SETTINGS")

            for k,v in self.shot_settings.items():
                new_settings.attrs.create(k, v)






        


        
