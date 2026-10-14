import time
import threading
import numpy as np
import labscript_utils.h5_lock
import h5py
import labscript_utils.properties
import zmq
import time
import json
from zprocess import RichStreamHandler

from blacs.tab_base_classes import Worker

from .dcam import *
from .dcamapi4 import *
import logging
from multiprocessing import shared_memory
from time import perf_counter
import datetime

from labscript_utils.ls_zprocess import Context

class OrcaQuestWorker(Worker):
    # additional properties passed down by the initialise_workers() method in blacs_tabs.py
    # - parent_host
    # - trigger_source
    # - attr_dict
    # - rearrange_active
    # - stop_acquisition_timeout
    # - image_receiver_port

    def init(self):
        self.logger = logging.getLogger("orcacam")
        self.logger.setLevel(logging.DEBUG)

        # Prevent duplicate handlers if this code runs more than once
        if not self.logger.handlers:
            self.logger.addHandler(RichStreamHandler())

        # Now use it in your code
        self.logger.info("Worker started at time: " + str(datetime.datetime.now()))

        self.image_shape = [2304,4096]
        self.image_dtype = "uint16"

        if Dcamapi.init():
            device_index = 0 #hardcoded Hamamatsu device index, but can make into a connection table property in the future if we start using more than one Hamamatsu device
            self.dcam = Dcam(device_index)
            if self.dcam.dev_open():
                self.dcam.prop_setvalue(DCAM_IDPROP.TRIGGERSOURCE, self.trigger_source)
                for key in self.attr_dict:
                    print(key, self.attr_dict[key])
                    self.dcam.prop_setvalue(getattr(DCAM_IDPROP,key,None), self.attr_dict[key])
                    if key == "SUBARRAYVSIZE":
                        self.image_shape[0] = self.attr_dict[key]
                    if key == "SUBARRAYHSIZE":
                        self.image_shape[1] = self.attr_dict[key]
                    if key == "BITSPERCHANNEL":
                        if self.attr_dict[key] == 16 or 12:
                            self.image_dtype = "uint16"
                        if self.attr_dict[key] == 8:
                            self.image_dtype = "uint8"
            else:
                self.logger.error('-NG: Dcam.dev_open() fails with error {}'.format(self.dcam.lasterr()))
        else:
            self.logger.error('-NG: Dcamapi.init() fails with error {}'.format(Dcamapi.lasterr()))

        self.logger.info("Camera Initialized at time: " + str(datetime.datetime.now()))

        self.images = []
        self.image_metadata = []
        self.n_images = None

        self.roi_mode = self.dcam.prop_getvalue(getattr(DCAM_IDPROP,'SUBARRAYMODE',None))
        self.exposure_time = 0.01

        self.exposures = None
        self.acquisition_thread = None
        self.h5_filepath = None

        self.mode_before_continuous_started = None
        self.continuous_stop = threading.Event()
        self.continuous_thread = None
        self.continuous_dt = None

        self.fetch_image_finished = threading.Event()
        self.acquisition_timed_out = threading.Event()

        self.zmq_context = zmq.Context()

        self.image_socket = self.zmq_context.socket(zmq.REQ)
        self.image_socket.connect(
            f'tcp://{self.parent_host}:{self.image_receiver_port}'
        )
        if self.rearrange_active:
            self.shm_frame = shared_memory.SharedMemory(create=True, size = 2*2304*4096, name = "camera_frame") #allocates shared memory (equal to the number of bytes needed to store a single OrcaQuest camera image at full resolution and dynamic range) for the rearrangment image, to be accessed by external rearrangement script
            self.shm_img = np.ndarray(tuple(self.image_shape), dtype=self.image_dtype, buffer=self.shm_frame.buf) #creates an empty array in the above shared memory to hold the image

            self.shm_flag = shared_memory.SharedMemory(create=True, size = 8, name = "img_flag") #allocates shared memory for a flag that updates when a new image is sent over
            self.shm_count = np.ndarray((1,), dtype=np.int64, buffer=self.shm_flag.buf) #creates an empty array (1D array with one element, which is an int64 counter) in the above shared memory to hold the flag
            self.shm_count[0] = -1

            with open("shm.json", "w") as f:
                json.dump([self.image_shape, self.image_dtype], f)
                self.logger.error("Saving json")

        self.for_rearrange = []

        # for prop in DCAM_IDPROP:
        #     self.logger.error(f"{prop.name}: {self.dcam.prop_getvalue(prop.value)}")
        

        self.logger.info("Initialization Finished at time: " + str(datetime.datetime.now()))


    def fetch_n_images(self):
        self.logger.error("fetch_n_images STARTED at time: " + str(datetime.datetime.now()))
        poll_timeout = 1 #polls in intervals of 1 ms, could this be longer?
        self.images=[]
        self.image_metadata = []

        self.logger.info('pre-snapshot cap status: {}'.format(self.dcam.cap_status()))
        if self.dcam.cap_snapshot():
            self.logger.info("camera snapshot started at time: " + str(datetime.datetime.now()))
            n = 0

            while len(self.images) < self.n_images:
                if (n%1000) == 0:
                    self.logger.info(f"Looking for image {len(self.images)}")
                    self.logger.info('cap status: {}'.format(self.dcam.cap_status()))
                n+=1
                
                if (self.acquisition_timed_out.is_set()):
                    self.logger.error('cap status: {}'.format(self.dcam.cap_status()))
                    self.logger.error(f'fails with error {self.dcam.lasterr()}')  
                    # self.dcam.cap_snapshot()
                    # self.logger.error('retry cap status: {}'.format(self.dcam.cap_status()))
                    break


                if self.dcam.wait_capevent_frameready(poll_timeout): #wait until the camera detects a capture event and then pulls from the buffer during the downtime of the main thread performing shot execution
                    ret = self.dcam.buf_getframe(len(self.images))
                    data = None
                    meta = None
                    if ret is not False:
                        meta = self.struct_to_dict(ret[0])
                        meta.pop("buf")
                        self.logger.info(f"Found metadata {meta} at time: " + str(datetime.datetime.now()))
                        data = ret[1]
                        self.images.append(data)
                        self.image_metadata.append(meta)
                        self.logger.info(f"capture #{len(self.images) - 1} acquired at time: " + str(datetime.datetime.now()))
                    else: 
                        dcamerr = self.dcam.lasterr()
                        self.logger.error(f"With len(self.images)={len(self.images)} ran into error {dcamerr}") 


                    if len(self.images) > 0 and self.for_rearrange[len(self.images) - 1]:

                        self.shm_img[:] = self.images[-1] #saves image to the array that lives in the correct shared memory (overwrites previous image in that array)

                        print(self.shm_img)
                        self.shm_count[0] = len(self.images) - 1 #updates "new image" flag in the correct shared memory

                        print("flag updated to " + str(self.shm_count[0]))


                else:
                    dcamerr = self.dcam.lasterr()
                    if dcamerr.is_timeout():
                        continue
                    else:
                        self.logger.error('-NG: Dcam.wait_event() fails with error {}'.format(dcamerr))
                        break
        else:
            print('-NG: Dcam.cap_start() fails with error {}'.format(self.dcam.lasterr()))

        self.fetch_image_finished.set() #for fetch_n_images(), call this method at the very end after the loop has fully completed and self.images is fully populated
        self.logger.error("fetch_n_images FINISHED at time: " + str(datetime.datetime.now()))

    def snap(self):
        """Acquire one frame in manual mode. Send it to the parent via
        self.image_socket. Wait for a response from the parent."""

        self.logger.error("snap STARTED at time: " + str(datetime.datetime.now()))
        self.images=[]
        self.image_metadata = []
        self.n_images = 1
        self.for_rearrange = [False]
        set_trigger = self.dcam.prop_setvalue(DCAM_IDPROP.TRIGGERSOURCE, DCAMPROP.TRIGGERSOURCE.INTERNAL) # Set it to internal triggering
        trigger_active = self.dcam.prop_setvalue(DCAM_IDPROP.TRIGGERACTIVE, DCAMPROP.TRIGGERACTIVE.EDGE) # Set it to internal triggering

        # if set_trigger:
        #     self.logger.info(f"ABLE TO SET TRIGGER_SOURCE TO 1, returned {set_trigger}")
        # else:
        #     self.logger.error(f"COULD NOT SET TRIGGER_SOURCE, returned {set_trigger} with error {self.dcam.lasterr()}")

        self.logger.info('cap status: {}'.format(self.dcam.cap_status()))
        if self.dcam.buf_alloc(self.n_images):
            self.acquisition_thread = threading.Thread(
            target=self.fetch_n_images,
            args=(),
            daemon=True,
            )

            # for prop in DCAM_IDPROP:
            #     self.logger.error(f"{prop.name}: {self.dcam.prop_getvalue(prop.value)}")

            self.acquisition_thread.start()

            self.logger.info('thread for snap single image started')
        else:
            self.logger.error('-NG: in snap(): Dcam.buf_alloc() fails with error {}'.format(self.dcam.lasterr()))
            return {}


        if self.fetch_image_finished.wait(self.stop_acquisition_timeout): #waits for a duration of self.stop_acquisition_timeout for the self.fetch_image_finished Event to be set, which occurs only after all images have been fetched and appended to self.images
            self.logger.info("fetch image thread finished")
            self.fetch_image_finished.clear()
            if self.dcam.buf_release():
                self.logger.info(f"buf_release succeeded")
            else:
                self.logger.info(f"buf_release error:{self.dcam.lasterr()}")
        else:
            self.acquisition_timed_out.set()
            self.logger.info(f"Timeout for fetch_image_finished")


        if len(self.images) > 0:
            self._send_image_to_parent(np.array(self.images), [b'snap'], self.get_roi())
        else:
            self.logger.error("unable to send image to parent, no images in self.images")

        cam_props = {}
        for prop in DCAM_IDPROP:
            cam_props[prop.name] = self.dcam.prop_getvalue(prop.value)
        
        cam_props = {k: v for k, v in sorted(cam_props.items(), key=lambda item: item[0])}

        # for k in cam_props:
        #     self.logger.error(f"{k:<30}: {cam_props[k]}")

        self.logger.error("snap FINISHED at time: " + str(datetime.datetime.now()))

    def _send_image_to_parent(self, image, names=[], roi=(0,4096,0,2304), filepath = None):
        """Send the image to the GUI to display. This will block if the parent process
        is lagging behind in displaying frames, in order to avoid a backlog."""

        metadata = dict(dtype=str(image.dtype), shape=image.shape)

        if filepath is not None:
            script = filepath.split("\\")[3]
            run_number = filepath.split('\\')[7]
            shot_number = filepath.split('_')[-1][:-3]
            run_label = f"{script}\t\\\t{run_number}\t\\\t{shot_number}\t\\\t"
        else:
            run_label = ""
        names = [run_label + name.decode('utf-8') for name in names]
        str_names = "#_#".join(names)
        str_roi = "#_#".join([str(r) for r in roi])

        self.image_socket.send_json(metadata, zmq.SNDMORE)
        self.image_socket.send_string(str_names, zmq.SNDMORE)
        self.image_socket.send_string(str_roi, zmq.SNDMORE)
        self.image_socket.send(image, copy=False)

        response = self.image_socket.recv()
        assert response == b'ok', response

    def continuous_loop(self, dt):
        poll_timeout = 1
        """Acquire continuously in a loop, with minimum repetition interval dt"""
        self.logger.info("continuous loop started")
        t = time.time()
        set_trigger = self.dcam.prop_setvalue(DCAM_IDPROP.TRIGGERSOURCE, DCAMPROP.TRIGGERSOURCE.INTERNAL) # Set it to internal triggering
        trigger_active = self.dcam.prop_setvalue(DCAM_IDPROP.TRIGGERACTIVE, DCAMPROP.TRIGGERACTIVE.EDGE) # Set it to internal triggering

        # if set_trigger:
        #     self.logger.info(f"ABLE TO SET TRIGGER_SOURCE TO 1, returned {set_trigger}")
        # else:
        #     self.logger.error(f"COULD NOT SET TRIGGER_SOURCE, returned {set_trigger} with error {self.dcam.lasterr()}")

        if self.dcam.buf_alloc(20) and self.dcam.cap_start(True):

            while True:
                t = time.time()
                
                if self.dcam.wait_capevent_frameready(poll_timeout): #wait until the camera detects a capture event and then pulls from the buffer during the downtime of the main thread performing shot execution
                    data = self.dcam.buf_getlastframedata() #data in the form of 2d numpy array

                    data = np.array(data)

                    self._send_image_to_parent(data, [b'continuous'], self.get_roi())
                    # self.logger.info(f"Sent image to parent at time {t}")

                    # self.logger.info('new capture acquired')
                else:
                    dcamerr = self.dcam.lasterr()
                    if dcamerr.is_timeout():
                        continue
                    else:
                        self.logger.error('-NG: Dcam.wait_event() fails with error {dcamerr}'.format(dcamerr))

                if dt is None:
                    timeout = 1e-6
                else:
                    timeout = t + dt - time.time()

                if self.continuous_stop.wait(timeout):
                    self.continuous_stop.clear()
                    self.logger.info(f"Continuous_stop event occured, aborting now at time: " + str(datetime.datetime.now()))
                    break
        else:
            self.logger.error('-NG: in continuous_loop(): Dcam.buf_alloc() fails with error {}'.format(self.dcam.lasterr()))
            return {}

    def start_continuous(self, dt):
        self.logger.info(f"start_continuous({dt}) Called at time: " + str(datetime.datetime.now()))
        """Begin continuous acquisition in a thread with minimum repetition interval
        dt"""
        # TODO: set and store trigger mode
        assert self.continuous_thread is None
        # self.mode_before_continuous_started = self.camera.feature_tree["Acquisition"]["AcquisitionMode"].feature.value
        # self.camera.feature_tree["Acquisition"]["AcquisitionMode"].feature.value = "Continuous"
        self.continuous_thread = threading.Thread(
            target=self.continuous_loop, args=(dt,), daemon=True
        )
        self.continuous_thread.start()
        self.continuous_dt = dt

        self.logger.info("continuous thread started at time: " + str(datetime.datetime.now()))

    def stop_continuous(self, pause=False):
        self.logger.info(f"stop_continuous({pause}) Called at time: " + str(datetime.datetime.now()))
        for prop in DCAM_IDPROP:
            self.logger.error(f"{prop.name}: {self.dcam.prop_getvalue(prop.value)}")
        """Stop the continuous acquisition thread"""
        if not self.continuous_thread:
            return
        self.continuous_stop.set()
        self.logger.info(f"continuous_stop event set at time: " + str(datetime.datetime.now()))
        self.continuous_thread.join() # self.continuous_dt * 5)
        self.continuous_thread = None

        self.logger.info("continuous thread stopped at time: " + str(datetime.datetime.now()))

        self.dcam.cap_stop()
        time.sleep(5e-3)
        self.dcam.buf_release()
        time.sleep(5e-3)
        set_trigger = self.dcam.prop_setvalue(DCAM_IDPROP.TRIGGERSOURCE, self.trigger_source)
        trigger_active = self.dcam.prop_setvalue(DCAM_IDPROP.TRIGGERACTIVE, DCAMPROP.TRIGGERACTIVE.LEVEL) # Set it to internal triggering

        if set_trigger:
            self.logger.info(f"ABLE TO SET TRIGGER_SOURCE TO {self.trigger_source}, returned {set_trigger}")
        else:
            self.logger.error(f"COULD NOT SET TRIGGER_SOURCE, returned {set_trigger} with error {self.dcam.lasterr()}")

        


        time.sleep(5e-3)
        # self.camera.stop_acquisition()
        # If we're just 'pausing', then do not clear self.continuous_dt. That way
        # continuous acquisition can be resumed with the same interval by calling
        # start(self.continuous_dt), without having to get the interval from the parent
        # again, and the fact that self.continuous_dt is not None can be used to infer
        # that continuous acquisiton is paused and should be resumed after a buffered
        # run is complete:
        if not pause:
            self.continuous_dt = None

    def log_str(self, log_string):
        self.logger.error(f"log_str: {log_string}")
        return True

    def enable_roi(self):
        ret = self.dcam.prop_setvalue(getattr(DCAM_IDPROP,'SUBARRAYMODE',None), 2)
        self.logger.info(f"enable_roi() Called: {ret}")
        return self.check_remote_values()

    def disable_roi(self):
        ret = self.dcam.prop_setvalue(getattr(DCAM_IDPROP,'SUBARRAYMODE',None), 1)
        self.logger.info(f"diable_roi() Called: {ret}")
        return self.check_remote_values()


    def update_exposure(self, exposure): # exposure in ms
        ret = False
        ret = self.dcam.prop_setvalue(getattr(DCAM_IDPROP,'EXPOSURETIME',None), exposure * 1e-3)
        
        self.logger.info(f"update_exposure({exposure}) Called: {ret}")
        return self.check_remote_values()

    def update_roi(self, hpos:int, hsize:int, vpos:int, vsize:int):
        ret = True
        self.logger.info(f"update_roi({hpos},{hsize},{vpos},{vsize}) called:")

        if hsize >= 0:
            ret &= self.dcam.prop_setvalue(getattr(DCAM_IDPROP,'SUBARRAYHSIZE',None), hsize)
            self.image_shape[1] = hsize
            self.logger.info(f"hsizeret: {ret}")

        if vsize >= 0:
            ret &= self.dcam.prop_setvalue(getattr(DCAM_IDPROP,'SUBARRAYVSIZE',None), vsize)
            self.image_shape[0] = vsize
            self.logger.info(f"vsizeret: {ret}")

        if hpos >= 0:
            ret &= self.dcam.prop_setvalue(getattr(DCAM_IDPROP,'SUBARRAYHPOS',None), hpos)
            self.logger.info(f"hposret: {ret}")

        if vpos >= 0:
            ret &= self.dcam.prop_setvalue(getattr(DCAM_IDPROP,'SUBARRAYVPOS',None), vpos)
            self.logger.info(f"vposret: {ret}")


        return self.check_remote_values()

    def transition_to_buffered(self, device_name, h5_filepath, initial_values, fresh):
        self.logger.error("transition_to_buffered STARTED at time: " + str(datetime.datetime.now()))


        if self.continuous_thread is not None:
            # Pause continuous acquistion during transition_to_buffered:
            self.stop_continuous(pause=False)

        set_trigger = self.dcam.prop_setvalue(DCAM_IDPROP.TRIGGERSOURCE, self.trigger_source)
        trigger_active = self.dcam.prop_setvalue(DCAM_IDPROP.TRIGGERACTIVE, DCAMPROP.TRIGGERACTIVE.LEVEL) # Set it to internal triggering

        if set_trigger:
            self.logger.info(f"ABLE TO SET TRIGGER_SOURCE TO {self.trigger_source}, returned {set_trigger}")
        else:
            self.logger.error(f"COULD NOT SET TRIGGER_SOURCE, returned {set_trigger} with error {self.dcam.lasterr()}")


        with h5py.File(h5_filepath, 'r+') as f:
            group = f['devices'][self.device_name]

            if 'NEW_SHOT_SETTINGS' in group:
                self.logger.info(f"new shot settings found")
                for k,v in group['NEW_SHOT_SETTINGS'].attrs.items():
                    if getattr(DCAM_IDPROP, k, None) is None:
                        raise ValueError(f"Could not find requested camera property {k}")
                    old_v = self.dcam.prop_getvalue(getattr(DCAM_IDPROP, k, None))
                    self.logger.info(f"Found old value for {k} of {old_v}")
                    self.dcam.prop_setvalue(getattr(DCAM_IDPROP,k,None), v)
                    self.logger.info(f"Setting new value for {k} of {v}")
                    group['OLD_SHOT_SETTINGS'].attrs.create(k, old_v)
            else:
                self.logger.info(f"new shot settings not found")

            if not 'EXPOSURES' in group:
                self.n_images = 0
                return {}

            self.h5_filepath = h5_filepath
            self.exposures = group['EXPOSURES'][:]
            self.n_images = len(self.exposures)

            self.logger.info(f"Using file {self.h5_filepath}")
            self.logger.info(f"Configuring camera for {self.n_images} images.")

            for exposure in self.exposures:
                self.for_rearrange.append(exposure[2].decode('utf-8') == "rearrange")
            
            self.logger.error(self.for_rearrange)

            min_trigger_interval =  self.dcam.prop_getvalue(getattr(DCAM_IDPROP,"TIMING_MINTRIGGERINTERVAL",None))
            exposure_times = np.array([exposure['t'] for exposure in self.exposures])
            for n in np.arange(len(exposure_times)) - 1:
                if 0 < exposure_times[n+1] - exposure_times[n] < min_trigger_interval:
                    raise ValueError(f"ERROR: Unable to trigger camera within {min_trigger_interval:.4f} s, images \"{self.exposures[n]['name']}\" and \"{self.exposures[n+1]['name']}\" are {exposure_times[n+1] - exposure_times[n]:.4f} s apart")


        self.fetch_image_finished.clear()
        self.acquisition_timed_out.clear()

        if self.rearrange_active:
            self.shm_count[0] = -1  

        if self.dcam.buf_alloc(self.n_images):

            self.acquisition_thread = threading.Thread(
            target=self.fetch_n_images,
            args=(),
            daemon=True,
            )

            self.acquisition_thread.start()

            self.logger.error("thread started at time: " + str(datetime.datetime.now()))
            self.logger.error("transition_to_buffered FINISHED at time: " + str(datetime.datetime.now()))
        else:
            self.logger.error('-NG: Dcam.buf_alloc() fails with error {}'.format(self.dcam.lasterr()))
    
        self.logger.info("END OF TRANSITION_TO_BUFFERED")
        for prop in DCAM_IDPROP:
            self.logger.error(f"{prop.name}: {self.dcam.prop_getvalue(prop.value)}")


        return {}
        

    def _image_fetch_callback(self, images):
        self.images = images
        self.fetch_image_finished.set()

    def transition_to_manual(self):
        self.logger.error("transition_to_manual STARTED at time: " + str(datetime.datetime.now()))

        for prop in DCAM_IDPROP:
            self.logger.error(f"{prop.name}: {self.dcam.prop_getvalue(prop.value)}")

        if self.h5_filepath is None:
            self.logger.info('No camera exposures in this shot.\n')
            return True

        if self.n_images == 0:
            assert self.acquisition_thread is None
            self.fetch_image_finished.set()
        else:
            assert self.acquisition_thread is not None

            self.logger.info(f"Waiting until fetch images finished for time {self.stop_acquisition_timeout}")
            if self.fetch_image_finished.wait(self.stop_acquisition_timeout): #waits for a duration of self.stop_acquisition_timeout for the self.fetch_image_finished Event to be set, which occurs only after all images have been fetched and appended to self.images
                self.fetch_image_finished.clear()
                self.dcam.buf_release()
            else:
                self.logger.error(f"RAN OUT OF PATIENCE")
                self.acquisition_timed_out.set()
                emsg = ("Acquisition thread did not finish. Likely did not acquire expected"
                        "number of images. Check triggering is connected/configured correctly.")

                self.logger.error(self.dcam.lasterr())
                self.dcam.cap_stop()
                self.logger.error(f"CAP STOPOED")
                self.abort()
                self.logger.error(f"ABORTED")
                raise RuntimeError(emsg)
            
            self.acquisition_thread = None

            # self.camera.stop_acquisition()
            # self.logger.log("Acquisition Thread Finished")

            roi = self.get_roi()

            with h5py.File(self.h5_filepath, 'r+') as f:
                group = f['devices'][self.device_name]

                self.logger.error(self.h5_filepath)
                image_path = 'images/' + self.device_name
                image_group = f.require_group(image_path)
                image_group.attrs['camera'] = self.device_name

                for prop in DCAM_IDPROP:
                    # self.logger.error(f"{prop.name}: {self.dcam.prop_getvalue(prop.value)}")
                    image_group.attrs[prop.name] = self.dcam.prop_getvalue(prop.value)

                labscript_utils.properties.set_attributes(image_group, {**self.attr_dict, **self.check_remote_values()})

                if 'OLD_SHOT_SETTINGS' in list(group.keys()):
                    for k,v in group['OLD_SHOT_SETTINGS'].attrs.items():
                        self.dcam.prop_setvalue(getattr(DCAM_IDPROP,k,None), v)
                        self.logger.info(f"Setting camera back to old value for {k} of {v}")

                # Whether we failed to get all the expected exposures:
                image_group.attrs['failed_shot'] = len(self.images) != len(self.exposures)

                # key the images by name and frametype. Allow for the case of there being
                # multiple images with the same name and frametype. In this case we will
                # save an array of images in a single dataset.
                images = {
                    (exposure['name'], exposure['frametype']): []
                    for exposure in self.exposures
                }
                metadatas = {
                    (exposure['name'], exposure['frametype']): []
                    for exposure in self.exposures
                }

                # Iterate over expected exposures, sorted by acquisition time, to match them
                # up with the acquired images:
                self.exposures.sort(order='t')
                names = []
                self.logger.info("starting zip")
                for image, meta, exposure in zip(self.images, self.image_metadata, self.exposures):
                    self.logger.info(f"exposure {exposure} with meta: {meta}")
                    if exposure['name'] == b'DISCARD' and exposure['frametype'] == b'DISCARD':
                        self.logger.debug(f"Discarding Sensor-clearing image at time {exposure['t']}")
                        images.pop((b'DISCARD', b'DISCARD'))
                        metadatas.pop((b'DISCARD', b'DISCARD'))
                    else:
                        images[(exposure['name'], exposure['frametype'])].append(image)
                        metadatas[(exposure['name'], exposure['frametype'])].append(meta)
                        names.append(exposure['name'])

                # Save images to the HDF5 file:
                for (name, frametype), imagelist in images.items():
                    data = imagelist[0] if len(imagelist) == 1 else np.array(imagelist)
                    self.logger.debug(f"Saving frame(s) {name}/{frametype}.")
                    group = image_group.require_group(name)
                    dset = group.create_dataset(
                        frametype, data=data.astype('uint16'), dtype='uint16', compression='gzip'
                    )
                    # Specify this dataset should be viewed as an image
                    dset.attrs['CLASS'] = np.bytes_('IMAGE')
                    dset.attrs['IMAGE_SUBCLASS'] = np.bytes_('IMAGE_GRAYSCALE')
                    metas = {}
                    for m in metadatas[(name, frametype)]:
                        metas = metas | m
                    for k,v in metas.items():
                        dset.attrs[k] = v

            self.logger.info(f"{len(images)} images saved with shape {np.shape(np.stack(self.images))}")
            # If the images are all the same shape, send them to the GUI for display:
            self.images = [self.images[i] for i in np.arange(len(self.images)) if not (self.exposures[i]['name'] == b'DISCARD' and self.exposures[i]['frametype'] == b'DISCARD')]
            try:
                image_block = np.stack(self.images)
                # self.logger.error(self.h5_filepath)
                self._send_image_to_parent(image_block, names, roi, filepath = self.h5_filepath)
            except ValueError:
                self.logger.warning("Cannot display images in the GUI, they are not all the same shape")
            else:
                self._send_image_to_parent(image_block, names, roi, filepath = self.h5_filepath)


            self.images = []
            self.image_metadata = []
            self.n_images = None
            self.exposures = None
            self.h5_filepath = None
            if self.rearrange_active:
                self.shm_count[0] = -1
            self.for_rearrange = []

        self.logger.error("transition_to_manual FINISHED at time: " + str(datetime.datetime.now()))
        return True

    def abort(self):
        if self.acquisition_thread is not None:
            self.acquisition_thread.join()
            self.acquisition_thread = None
            self.logger.error("Abort triggered acquisition_thread joining")
        self.dcam.buf_release()
        self.acquisition_thread = None
        self.images = []
        self.image_metadata = []
        self.n_images = None
        self.exposures = None
        self.acquisition_thread = None
        self.h5_filepath = None
        self.stop_acquisition_timeout = None
        self.exception_on_failed_shot = None

        # Resume continuous acquisition, if any:
        if self.continuous_dt is not None and self.continuous_thread is None:
            self.start_continuous(self.continuous_dt)
        return True

    def abort_buffered(self):
        return self.abort()

    def abort_transition_to_buffered(self):
        return self.abort()

    def check_remote_values(self):
        results = {}

        results['exposure'] = self.dcam.prop_getvalue(getattr(DCAM_IDPROP,'EXPOSURETIME',None)) * 1e3
        
        results['roi_mode'] = self.dcam.prop_getvalue(getattr(DCAM_IDPROP,'SUBARRAYMODE',None))

        results['hpos']     = self.dcam.prop_getvalue(getattr(DCAM_IDPROP,'SUBARRAYHPOS',None))
        results['hsize']    = self.dcam.prop_getvalue(getattr(DCAM_IDPROP,'SUBARRAYHSIZE',None))
        results['vpos']     = self.dcam.prop_getvalue(getattr(DCAM_IDPROP,'SUBARRAYVPOS',None))
        results['vsize']    = self.dcam.prop_getvalue(getattr(DCAM_IDPROP,'SUBARRAYVSIZE',None))
        results['status']   = True

        self.logger.info(f"remote values: {results}")
        return results

    def get_roi(self):
        if self.dcam.prop_getvalue(getattr(DCAM_IDPROP,'SUBARRAYMODE',None)) == 2:
            hpos  = int(self.dcam.prop_getvalue(getattr(DCAM_IDPROP,'SUBARRAYHPOS',None)))
            hsize = int(self.dcam.prop_getvalue(getattr(DCAM_IDPROP,'SUBARRAYHSIZE',None)))
            vpos  = int(self.dcam.prop_getvalue(getattr(DCAM_IDPROP,'SUBARRAYVPOS',None)))
            vsize = int(self.dcam.prop_getvalue(getattr(DCAM_IDPROP,'SUBARRAYVSIZE',None)))
            return (hpos, hsize, vpos, vsize)
        else:
            return (0, 4096, 0, 2304)

    def program_manual(self, values):
        
        self.update_exposure(values.get('exposure', 10))
        
        if values.get('roi_mode', 1) == 2:
            self.enable_roi()
        else:
            self.disable_roi()

        self.update_roi(
            values.get('hpos', 0),
            values.get('hsize', 4096),
            values.get('vpos', 0),
            values.get('vsize', 2304)
        )

        return self.check_remote_values()

    def shutdown(self):
        if self.continuous_thread is not None:
            self.stop_continuous()

        self.image_socket.close()  # forgetting to close the socket will crash blacs when restarting worker
        self.dcam.cap_stop()
        self.dcam.buf_release()
        self.dcam.dev_close()
        Dcamapi.uninit()

    def struct_to_dict(self, struct):
        if not isinstance(struct, Structure):
            return struct
        d = {}
        for field, _ in struct._fields_:
            sub_dict = self.struct_to_dict(struct.__getattribute__(field))
            if isinstance(sub_dict, dict):
                for k,v in sub_dict.items():
                    d[field + "_" + k] = v
            else:
                d[field] = sub_dict
        return d

