import os
import qtutils.icons
import numpy as np
from zprocess import RichStreamHandler
import logging

from PyQt5 import uic, QtWidgets, QtCore, QtGui
from PyQt5.QtCore import QTimer, pyqtSignal

import pyqtgraph as pg



class OrcaWidget(QtWidgets.QWidget):
    # signals emitted by this widget to outside world
    on_continuous_requested = pyqtSignal()
    on_stop_requested = pyqtSignal()
    on_snap_requested = pyqtSignal()
    on_continuous_max_change_requested = pyqtSignal(float)
    on_exposure_change_requested = pyqtSignal(bool)
        # roi
    on_roi_enable_requested = pyqtSignal()
    on_roi_disable_requested = pyqtSignal()
    on_roi_change_requested = pyqtSignal(object, object, object, object)
    on_log_str_requested = pyqtSignal(str)
    # signals this widget received
    request_enter_continuous_mode = pyqtSignal()
    request_exit_continuous_mode = pyqtSignal()
    request_update_colormap = pyqtSignal(object)
    request_update_image = pyqtSignal(object)
    request_update_fps = pyqtSignal(float)
    request_update_max_fps = pyqtSignal(float)
    request_update_labels = pyqtSignal(str)
    request_update_cur_image_roi = pyqtSignal(str)
    request_shutdown = pyqtSignal()
    request_update_exposure = pyqtSignal(float)
    request_enable_roi = pyqtSignal()
    request_disable_roi = pyqtSignal()
    request_roi_update = pyqtSignal(object, object, object, object)
    request_update_status = pyqtSignal(bool)
    request_click_roiBtn = pyqtSignal()
    

    def __init__(self, parent, device_name):
        super().__init__(parent)
        
        self.device_name = device_name
        self.image_names = []
        self.cur_image_roi = (0,4096,0,2304)
        self.status = False
        self.exposure_changed_programatically = False

        ui_filepath = os.path.join(
            os.path.dirname(os.path.realpath(__file__)), 'blacs_tab.ui'
        )
        uic.loadUi(ui_filepath, self)

        # emit signals to outside world
        self.continuousButton.clicked.connect(lambda: self.on_continuous_requested.emit())
        self.stopButton.clicked.connect(lambda: self.on_stop_requested.emit())
        self.snapButton.clicked.connect(lambda: self.on_snap_requested.emit())
        self.noMaxButton.clicked.connect(self.on_reset_rate_clicked)
        self.maxRateSpinBox.valueChanged.connect(lambda max_rate: self.on_continuous_max_change_requested.emit(max_rate))
        self.exposureSpinBox.valueChanged.connect(lambda _,b=self.exposure_changed_programatically: self.on_exposure_change_requested.emit(b))
        self.enableRoiButton.clicked.connect(lambda: self.on_roi_enable_requested.emit())
        self.disableRoiButton.clicked.connect(lambda: self.on_roi_disable_requested.emit())
        self.hposBox.valueChanged.connect(lambda  _:  self.on_roi_change_requested.emit(self.hpos, self.hsize, self.vpos, self.vsize) if not self.roi_changed_programmatically else None)
        self.hsizeBox.valueChanged.connect(lambda _:  self.on_roi_change_requested.emit(self.hpos, self.hsize, self.vpos, self.vsize) if not self.roi_changed_programmatically else None)
        self.vposBox.valueChanged.connect(lambda  _:  self.on_roi_change_requested.emit(self.hpos, self.hsize, self.vpos, self.vsize) if not self.roi_changed_programmatically else None)
        self.vsizeBox.valueChanged.connect(lambda _:  self.on_roi_change_requested.emit(self.hpos, self.hsize, self.vpos, self.vsize) if not self.roi_changed_programmatically else None)


        # signal outside world sends to this widget
        self.request_update_image.connect(self.update_image)
        self.request_update_fps.connect(self.update_fps)
        self.request_update_colormap.connect(self.set_colormap)
        self.request_update_max_fps.connect(self.set_max_fps)
        self.request_update_labels.connect(self.update_labels)
        self.request_update_cur_image_roi.connect(self.update_cur_image_roi)
        self.request_enter_continuous_mode.connect(self.setup_continuous_mode)
        self.request_exit_continuous_mode.connect(self.exit_continuous_mode)
        self.request_shutdown.connect(self.shutdown)
        self.request_update_exposure.connect(self.set_exposure)
        self.request_enable_roi.connect(self.enable_roi)
        self.request_disable_roi.connect(self.disable_roi)
        self.request_roi_update.connect(self.update_roi)
        self.request_update_status.connect(self.update_status)
        self.request_click_roiBtn.connect(self.click_roi_button)

        self.image = pg.ImageView()
        self.image.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        self.verticalLayout4.addWidget(self.image)

        self.image.roi.sigRegionChangeFinished.connect(self.pull_from_roi)
        # self.image.ui.roiBtn.clicked.connect(self.pull_from_roi)

        icon_height = 16
        self.statusGood.setPixmap(QtGui.QPixmap(':/qtutils/fugue/tick.png').scaledToHeight(icon_height))
        self.statusBad.setPixmap(QtGui.QPixmap(':/qtutils/fugue/cross.png').scaledToHeight(icon_height))


        self.stopButton.hide()
        self.maxRateSpinBox.hide()
        self.noMaxButton.hide()
        self.fpsLabel.hide()
        self.imageLabel.hide()
        self.statusGood.hide()
        self.hposBox.setEnabled(False)
        self.hsizeBox.setEnabled(False)
        self.vsizeBox.setEnabled(False)
        self.vposBox.setEnabled(False)

        self.roi_mode = 1
        self.disable_roi()
        self.roi_changed_programmatically = False

        self.label_update_timer = QTimer()
        self.label_update_timer.timeout.connect(self.update_cur_image_label)

        self.start_timer()


        # Ensure the GUI reserves space for these widgets even if they are hidden.
        # This prevents the GUI jumping around when buttons are clicked:
        for widget in [
            self.stopButton,
            self.maxRateSpinBox,
            self.noMaxButton,
        ]:
            size_policy = widget.sizePolicy()
            if hasattr(size_policy, 'setRetainSizeWhenHidden'): # Qt 5.2+ only
                size_policy.setRetainSizeWhenHidden(True)
                widget.setSizePolicy(size_policy)

    @property
    def max_fps(self):
        return self.maxRateSpinBox.value()

    @property
    def exposure(self):
        return self.exposureSpinBox.value()

    @property
    def hpos(self):
        return self.hposBox.value()
    
    @property
    def hsize(self):
        return self.hsizeBox.value()
    
    @property
    def vpos(self):
        return self.vposBox.value()
    
    @property
    def vsize(self):
        return self.vsizeBox.value()

    @property
    def colormap(self):
        return self.image.ui.histogram.gradient.saveState()

    def on_reset_rate_clicked(self):
        self.maxRateSpinBox.setValue(400)

    def update_image(self, image):
        old_index = self.image.currentIndex
        
        if self.image.image is None:
            self.image.setImage(image.swapaxes(-1, -2))
        else:
            self.image.setImage(
                image.swapaxes(-1, -2), autoRange=False, autoLevels=False
            )
        if len(image.shape) == 3 and old_index < image.shape[0]:
            self.image.setCurrentIndex(old_index)

        self.image.ui.roiPlot.setVisible(False)

        # draw immediately
        QtWidgets.QApplication.instance().sendPostedEvents()

    def update_cur_image_label(self):
        if self.image.currentIndex < len(self.image_names):
            self.imageLabel.show()
            self.imageLabel.setText(f"\"{self.image_names[self.image.currentIndex]}\"")
        else:
            self.imageLabel.hide()

    def update_labels(self, names):
        self.image_names = names.split("#_#")

    def update_cur_image_roi(self, rois):
        rois = rois.split("#_#")
        try:
            hpos = int(rois[0])
            hsize = int(rois[1])
            vpos = int(rois[2])
            vsize = int(rois[3])

            hsize = (np.clip(hsize, 4, 4096)          // 4) * 4
            vsize = (np.clip(vsize, 4, 2304)          // 4) * 4
            hpos  = (np.clip(hpos , 0, 4096 - hsize)  // 4) * 4
            vpos  = (np.clip(vpos , 0, 2304 - vsize)  // 4) * 4
            roi = (hpos, hsize, vpos, vsize)
            
            # if self.cur_image_roi != roi:
            #     self.roi_changed_programmatically = True
            #     self.image.roi.setPos(0,0)
            #     self.click_roi_button()
            #     self.roi_changed_programmatically = False

            self.cur_image_roi = roi
        except:
            self.cur_image_roi = (-1, -1, -1, -1)

    def update_fps(self, fps):
        self.fpsLabel.setText(f"{fps:.01f} fps")

    def setup_continuous_mode(self):
        self.snapButton.setEnabled(False)

        self.continuousButton.hide()

        self.exposureSpinBox.setEnabled(False)
        self.enableRoiButton.setEnabled(False)
        self.disableRoiButton.setEnabled(False)
        self.hposBox.setEnabled(False)
        self.hsizeBox.setEnabled(False)
        self.vposBox.setEnabled(False)
        self.vsizeBox.setEnabled(False)

        self.stopButton.show()
        self.maxRateSpinBox.show()
        self.noMaxButton.show()
        self.fpsLabel.show()
        self.fpsLabel.setText('? fps')

    def exit_continuous_mode(self):
        self.snapButton.setEnabled(True)

        self.continuousButton.show()

        self.exposureSpinBox.setEnabled(True)
        self.enableRoiButton.setEnabled(True)
        self.disableRoiButton.setEnabled(True)
        if self.roi_mode == 2:
            self.hposBox.setEnabled(True)
            self.hsizeBox.setEnabled(True)
            self.vposBox.setEnabled(True)
            self.vsizeBox.setEnabled(True)
        else:
            self.hposBox.setEnabled(False)
            self.hsizeBox.setEnabled(False)
            self.vposBox.setEnabled(False)
            self.vsizeBox.setEnabled(False)

        self.maxRateSpinBox.hide()
        self.noMaxButton.hide()
        self.stopButton.hide()
        self.fpsLabel.hide()

    def click_roi_button(self):
        if self.image.image is not None:
            self.image.roiClicked()

    def pull_from_roi(self):
        self.on_log_str_requested.emit(f"WIDGET: pull_from_roi()")
        showROI = False
        if self.image.image is not None:
            imROI = self.image.roi
            imSize = np.shape(self.image.image)
            showROI = self.image.ui.roiBtn.isChecked()# and imSize[-2] == 4096 and imSize[-1] == 2304
            # showROI |= self.image.ui.roiBtn.isChecked() and len(imSize) == 3 and imSize[1] == 4096 and imSize[2] == 2304
        self.on_log_str_requested.emit(f"imSize: {imSize} {showROI}")
        if showROI and not self.roi_changed_programmatically:
            if imROI.angle() != 0:
                imROI.setAngle(0)
            self.on_log_str_requested.emit(f"WIDGET: showROI and not roi_changed_programatically")
            self.on_log_str_requested.emit(f"WIDGET:imSize: {imSize}")
            self.roi_changed_programmatically = True
            hpos  = round(imROI.pos().x())
            hsize = round(imROI.size().x())
            vpos  = round(imROI.pos().y())
            vsize = round(imROI.size().y())

            if self.cur_image_roi != (-1,-1,-1,-1):
                hpos += self.cur_image_roi[0]
                vpos += self.cur_image_roi[2]

            self.on_log_str_requested.emit(f"WIDGET: {self.cur_image_roi} cur_image_roi")
            self.on_log_str_requested.emit(f"WIDGET: {hpos}, {hsize}, {vpos}, {vsize} pre-clip")

            hsize = (np.clip(hsize, 4, 4096)          // 4) * 4
            vsize = (np.clip(vsize, 4, 2304)          // 4) * 4
            hpos  = (np.clip(hpos , 0, 4096 - hsize)  // 4) * 4
            vpos  = (np.clip(vpos , 0, 2304 - vsize)  // 4) * 4

            self.hsizeBox.setValue(hsize)
            self.vsizeBox.setValue(vsize)
            self.hposBox.setValue(hpos)
            self.vposBox.setValue(vpos)
            
            self.on_roi_change_requested.emit(hpos, hsize, vpos, vsize)

        self.roi_changed_programmatically = False

    def enable_roi(self):
        self.on_log_str_requested.emit(f"WIDGET: enable_roi()")
        self.roi_mode = 2
        self.enableRoiButton.hide()

        self.disableRoiButton.show()
        self.hposBox.setEnabled(True)
        self.hsizeBox.setEnabled(True)
        self.vposBox.setEnabled(True)
        self.vsizeBox.setEnabled(True)


    def disable_roi(self):
        self.on_log_str_requested.emit(f"WIDGET: disable_roi()")
        self.roi_mode = 1
        self.enableRoiButton.show()

        self.disableRoiButton.hide()
        self.hposBox.setEnabled(False)
        self.hsizeBox.setEnabled(False)
        self.vposBox.setEnabled(False)
        self.vsizeBox.setEnabled(False)

    def update_roi(self, hpos, hsize, vpos, vsize):
        self.on_log_str_requested.emit(f"WIDGET: update_roi({hpos},{hsize},{vpos},{vsize})")
        showROI = False
        imROI = None

        self.roi_changed_programmatically = True

        self.hsizeBox.setValue(round(hsize))
        self.vsizeBox.setValue(round(vsize))
        self.hposBox.setValue(round(hpos))
        self.vposBox.setValue(round(vpos))

        if self.image.image is not None:
            imROI = self.image.roi
            imSize = np.shape(self.image.image)
            showROI = self.image.ui.roiBtn.isChecked() and self.roi_mode == 2

        if showROI:
            if imROI.angle() != 0:
                imROI.setAngle(0)
            if (imROI.size().x() != hsize or imROI.size().y() != vsize):
                imROI.setSize((hsize, vsize), center=pg.Point(0,0))
            if (imROI.pos().x() != hpos or imROI.pos().y() != vpos):
                imROI.setPos(hpos - self.cur_image_roi[0], vpos - self.cur_image_roi[2])
        
        self.roi_changed_programmatically = False

    def update_status(self, status):
        self.on_log_str_requested.emit(f"WIDGET: update_status({status})")
        self.status = status
        if status:
            self.statusGood.show()
            self.statusBad.hide()
        else:
            self.statusGood.hide()
            self.statusBad.show()

    def set_colormap(self, colormap):
        self.image.ui.histogram.gradient.restoreState(colormap)

    def set_max_fps(self, rate):
        self.maxRateSpinBox.setValue(rate)

    def set_exposure(self, exposure):
        self.on_log_str_requested.emit(f"WIDGET: set_exposure({exposure})")
        self.exposureSpinBox.setValue(exposure)

    def start_timer(self):
        self.label_update_timer.start(20) # ms

    def stop_timer(self):
        self.label_update_timer.stop()

    def shutdown(self):
        if self.label_update_timer is not None:
            self.stop_timer()
            self.label_update_timer = None
