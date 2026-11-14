import os
import qtutils.icons
import numpy as np
from zprocess import RichStreamHandler
import logging

from PySide6.QtUiTools import QUiLoader
from PySide6 import QtWidgets, QtGui
from PySide6.QtWidgets import QSizePolicy, QLayout
from PySide6.QtCore import QTimer, Signal

import pyqtgraph as pg



class OrcaWidget(QtWidgets.QWidget):
    # signals emitted by this widget to outside world
    on_continuous_requested = Signal()
    on_stop_requested = Signal()
    on_snap_requested = Signal()
    on_continuous_max_change_requested = Signal(float)
    on_exposure_change_requested = Signal(bool)
        # roi
    on_roi_enable_requested = Signal()
    on_roi_disable_requested = Signal()
    on_roi_change_requested = Signal(object, object, object, object)
    on_log_str_requested = Signal(str)
    # signals this widget received
    request_enter_continuous_mode = Signal()
    request_exit_continuous_mode = Signal()
    request_update_colormap = Signal(object)
    request_update_image = Signal(object)
    request_update_fps = Signal(float)
    request_update_max_fps = Signal(float)
    request_update_labels = Signal(str)
    request_update_cur_image_roi = Signal(str)
    request_shutdown = Signal()
    request_update_exposure = Signal(float)
    request_enable_roi = Signal()
    request_disable_roi = Signal()
    request_roi_update = Signal(object, object, object, object)
    request_update_status = Signal(bool)
    request_click_roiBtn = Signal()
    

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
        loader = QUiLoader()
        self.ui = loader.load(ui_filepath, self)
        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(self.ui)

        # emit signals to outside world
        self.ui.continuousButton.clicked.connect(lambda: self.on_continuous_requested.emit())
        self.ui.stopButton.clicked.connect(lambda: self.on_stop_requested.emit())
        self.ui.snapButton.clicked.connect(lambda: self.on_snap_requested.emit())
        self.ui.noMaxButton.clicked.connect(self.on_reset_rate_clicked)
        self.ui.maxRateSpinBox.valueChanged.connect(lambda max_rate: self.on_continuous_max_change_requested.emit(max_rate))
        self.ui.exposureSpinBox.valueChanged.connect(lambda _,b=self.exposure_changed_programatically: self.on_exposure_change_requested.emit(b))
        self.ui.enableRoiButton.clicked.connect(lambda: self.on_roi_enable_requested.emit())
        self.ui.disableRoiButton.clicked.connect(lambda: self.on_roi_disable_requested.emit())
        self.ui.hposBox.valueChanged.connect(lambda  _:  self.on_roi_change_requested.emit(self.hpos, self.hsize, self.vpos, self.vsize) if not self.roi_changed_programmatically else None)
        self.ui.hsizeBox.valueChanged.connect(lambda _:  self.on_roi_change_requested.emit(self.hpos, self.hsize, self.vpos, self.vsize) if not self.roi_changed_programmatically else None)
        self.ui.vposBox.valueChanged.connect(lambda  _:  self.on_roi_change_requested.emit(self.hpos, self.hsize, self.vpos, self.vsize) if not self.roi_changed_programmatically else None)
        self.ui.vsizeBox.valueChanged.connect(lambda _:  self.on_roi_change_requested.emit(self.hpos, self.hsize, self.vpos, self.vsize) if not self.roi_changed_programmatically else None)


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
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.ui.verticalLayout4.addWidget(self.image)



        self.image.roi.sigRegionChangeFinished.connect(self.pull_from_roi)
        # self.image.ui.roiBtn.clicked.connect(self.pull_from_roi)

        icon_height = 16
        self.ui.statusGood.setPixmap(QtGui.QPixmap(':/qtutils/fugue/tick.png').scaledToHeight(icon_height))
        self.ui.statusBad.setPixmap(QtGui.QPixmap(':/qtutils/fugue/cross.png').scaledToHeight(icon_height))


        self.ui.stopButton.hide()
        self.ui.maxRateSpinBox.hide()
        self.ui.noMaxButton.hide()
        self.ui.fpsLabel.hide()
        self.ui.imageLabel.hide()
        self.ui.statusGood.hide()
        self.ui.hposBox.setEnabled(False)
        self.ui.hsizeBox.setEnabled(False)
        self.ui.vsizeBox.setEnabled(False)
        self.ui.vposBox.setEnabled(False)

        self.roi_mode = 1
        self.disable_roi()
        self.roi_changed_programmatically = False

        self.label_update_timer = QTimer()
        self.label_update_timer.timeout.connect(self.update_cur_image_label)

        self.start_timer()


        # Ensure the GUI reserves space for these widgets even if they are hidden.
        # This prevents the GUI jumping around when buttons are clicked:
        for widget in [
            self.ui.stopButton,
            self.ui.maxRateSpinBox,
            self.ui.noMaxButton,
        ]:
            size_policy = widget.sizePolicy()
            if hasattr(size_policy, 'setRetainSizeWhenHidden'): # Qt 5.2+ only
                size_policy.setRetainSizeWhenHidden(True)
                widget.setSizePolicy(size_policy)

    @property
    def max_fps(self):
        return self.ui.maxRateSpinBox.value()

    @property
    def exposure(self):
        return self.ui.exposureSpinBox.value()

    @property
    def hpos(self):
        return self.ui.hposBox.value()
    
    @property
    def hsize(self):
        return self.ui.hsizeBox.value()
    
    @property
    def vpos(self):
        return self.ui.vposBox.value()
    
    @property
    def vsize(self):
        return self.ui.vsizeBox.value()

    @property
    def colormap(self):
        return self.image.ui.histogram.gradient.saveState()

    def on_reset_rate_clicked(self):
        self.ui.maxRateSpinBox.setValue(400)

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
            self.ui.imageLabel.show()
            self.ui.imageLabel.setText(f"\"{self.image_names[self.image.currentIndex]}\"")
        else:
            self.ui.imageLabel.hide()

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
        self.ui.fpsLabel.setText(f"{fps:.01f} fps")

    def setup_continuous_mode(self):
        self.ui.snapButton.setEnabled(False)

        self.ui.continuousButton.hide()

        self.ui.exposureSpinBox.setEnabled(False)
        self.ui.enableRoiButton.setEnabled(False)
        self.ui.disableRoiButton.setEnabled(False)
        self.ui.hposBox.setEnabled(False)
        self.ui.hsizeBox.setEnabled(False)
        self.ui.vposBox.setEnabled(False)
        self.ui.vsizeBox.setEnabled(False)

        self.ui.stopButton.show()
        self.ui.maxRateSpinBox.show()
        self.ui.noMaxButton.show()
        self.ui.fpsLabel.show()
        self.ui.fpsLabel.setText('? fps')

    def exit_continuous_mode(self):
        self.ui.snapButton.setEnabled(True)

        self.ui.continuousButton.show()

        self.ui.exposureSpinBox.setEnabled(True)
        self.ui.enableRoiButton.setEnabled(True)
        self.ui.disableRoiButton.setEnabled(True)
        if self.roi_mode == 2:
            self.ui.hposBox.setEnabled(True)
            self.ui.hsizeBox.setEnabled(True)
            self.ui.vposBox.setEnabled(True)
            self.ui.vsizeBox.setEnabled(True)
        else:
            self.ui.hposBox.setEnabled(False)
            self.ui.hsizeBox.setEnabled(False)
            self.ui.vposBox.setEnabled(False)
            self.ui.vsizeBox.setEnabled(False)

        self.ui.maxRateSpinBox.hide()
        self.ui.noMaxButton.hide()
        self.ui.stopButton.hide()
        self.ui.fpsLabel.hide()

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

            self.ui.hsizeBox.setValue(hsize)
            self.ui.vsizeBox.setValue(vsize)
            self.ui.hposBox.setValue(hpos)
            self.ui.vposBox.setValue(vpos)
            
            self.on_roi_change_requested.emit(hpos, hsize, vpos, vsize)

        self.roi_changed_programmatically = False

    def enable_roi(self):
        self.on_log_str_requested.emit(f"WIDGET: enable_roi()")
        self.roi_mode = 2
        self.ui.enableRoiButton.hide()

        self.ui.disableRoiButton.show()
        self.ui.hposBox.setEnabled(True)
        self.ui.hsizeBox.setEnabled(True)
        self.ui.vposBox.setEnabled(True)
        self.ui.vsizeBox.setEnabled(True)


    def disable_roi(self):
        self.on_log_str_requested.emit(f"WIDGET: disable_roi()")
        self.roi_mode = 1
        self.ui.enableRoiButton.show()

        self.ui.disableRoiButton.hide()
        self.ui.hposBox.setEnabled(False)
        self.ui.hsizeBox.setEnabled(False)
        self.ui.vposBox.setEnabled(False)
        self.ui.vsizeBox.setEnabled(False)

    def update_roi(self, hpos, hsize, vpos, vsize):
        self.on_log_str_requested.emit(f"WIDGET: update_roi({hpos},{hsize},{vpos},{vsize})")
        showROI = False
        imROI = None

        self.roi_changed_programmatically = True

        self.ui.hsizeBox.setValue(round(hsize))
        self.ui.vsizeBox.setValue(round(vsize))
        self.ui.hposBox.setValue(round(hpos))
        self.ui.vposBox.setValue(round(vpos))

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
            self.ui.statusGood.show()
            self.ui.statusBad.hide()
        else:
            self.ui.statusGood.hide()
            self.ui.statusBad.show()

    def set_colormap(self, colormap):
        self.image.ui.histogram.gradient.restoreState(colormap)

    def set_max_fps(self, rate):
        self.ui.maxRateSpinBox.setValue(rate)

    def set_exposure(self, exposure):
        self.on_log_str_requested.emit(f"WIDGET: set_exposure({exposure})")
        self.ui.exposureSpinBox.setValue(exposure)

    def start_timer(self):
        self.label_update_timer.start(20) # ms

    def stop_timer(self):
        self.label_update_timer.stop()

    def shutdown(self):
        if self.label_update_timer is not None:
            self.stop_timer()
            self.label_update_timer = None
