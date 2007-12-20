# Copyright (C) 2007, One Laptop Per Child
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

import os
import logging
from gettext import gettext as _
import time
import gtk

from xpcom.nsError import *
from xpcom import components
from xpcom.components import interfaces
from xpcom.server.factory import Factory
import dbus

from sugar.datastore import datastore
from sugar import profile
from sugar import mime
from sugar.graphics.alert import Alert, TimeoutAlert
from sugar.graphics.icon import Icon
from sugar.activity import activity

# #3903 - this constant can be removed and assumed to be 1 when dbus-python
# 0.82.3 is the only version used
import dbus
if dbus.version >= (0, 82, 3):
    DBUS_PYTHON_TIMEOUT_UNITS_PER_SECOND = 1
else:
    DBUS_PYTHON_TIMEOUT_UNITS_PER_SECOND = 1000

NS_BINDING_ABORTED = 0x804b0002     # From nsNetError.h

DS_DBUS_SERVICE = 'org.laptop.sugar.DataStore'
DS_DBUS_INTERFACE = 'org.laptop.sugar.DataStore'
DS_DBUS_PATH = '/org/laptop/sugar/DataStore'

_MIN_TIME_UPDATE = 5        # In seconds
_MIN_PERCENT_UPDATE = 10

_browser = None
_activity = None
_temp_path = '/tmp'
def init(browser, activity, temp_path):
    global _browser
    _browser = browser

    global _activity
    _activity = activity
    
    global _temp_path
    _temp_path = temp_path

_active_downloads = []

def can_quit():
    return len(_active_downloads) == 0

def remove_all_downloads():
    for download in _active_downloads:
        download._cancelable.cancel(NS_ERROR_FAILURE) 
        if download._dl_jobject is not None:
            download._datastore_deleted_handler.remove()
            datastore.delete(download._dl_jobject.object_id)
            download._cleanup_datastore_write()        

class DownloadManager:
    _com_interfaces_ = interfaces.nsIHelperAppLauncherDialog

    def promptForSaveToFile(self, launcher, window_context,
                            default_file, suggested_file_extension):
        file_class = components.classes["@mozilla.org/file/local;1"]
        dest_file = file_class.createInstance(interfaces.nsILocalFile)

        if not default_file:
            default_file = time.time()
            if suggested_file_extension:
                default_file = '%s.%s' % (default_file, suggested_file_extension)

        global _temp_path
        if not os.path.exists(_temp_path):
            os.makedirs(_temp_path)
        file_path = os.path.join(_temp_path, default_file)

        print file_path
        dest_file.initWithPath(file_path)
        
        return dest_file
                            
    def show(self, launcher, context, reason):
        launcher.saveToDisk(None, False)
        return NS_OK

components.registrar.registerFactory('{64355793-988d-40a5-ba8e-fcde78cac631}"',
                                     'Sugar Download Manager',
                                     '@mozilla.org/helperapplauncherdialog;1',
                                     Factory(DownloadManager))

class Download:
    _com_interfaces_ = interfaces.nsITransfer
    
    def init(self, source, target, display_name, mime_info, start_time,
             temp_file, cancelable):
        self._source = source
        self._mime_type = mime_info.MIMEType
        self._temp_file = temp_file
        self._target_file = target.queryInterface(interfaces.nsIFileURL).file
        self._cancelable = cancelable
        
        self._dl_jobject = None
        self._object_id = None
        self._last_update_time = 0
        self._last_update_percent = 0
        self._stop_alert = None
        
        return NS_OK

    def onStatusChange(self, web_progress, request, status, message):
        logging.info('Download.onStatusChange(%r, %r, %r, %r)' % \
            (web_progress, request, status, message))

    def onStateChange(self, web_progress, request, state_flags, status):
        if state_flags == interfaces.nsIWebProgressListener.STATE_START:
            self._create_journal_object()            
            self._object_id = self._dl_jobject.object_id
            
            alert = TimeoutAlert(9)
            alert.props.title = _('Download started')
            path, file_name = os.path.split(self._target_file.path)
            alert.props.msg = _('%s'%(file_name)) 
            _activity.add_alert(alert)
            alert.connect('response', self.__start_response_cb)
            alert.show()
            global _active_downloads
            _active_downloads.append(self)
            
        elif state_flags == interfaces.nsIWebProgressListener.STATE_STOP:
            if NS_FAILED(status): # download cancelled
                return

            self._stop_alert = Alert()
            self._stop_alert.props.title = _('Download completed') 
            path, file_name = os.path.split(self._target_file.path) 
            self._stop_alert.props.msg = _('%s'%(file_name)) 
            open_icon = Icon(icon_name='zoom-activity') 
            self._stop_alert.add_button(gtk.RESPONSE_APPLY, _('Open'), open_icon) 
            open_icon.show() 
            ok_icon = Icon(icon_name='dialog-ok') 
            self._stop_alert.add_button(gtk.RESPONSE_OK, _('Ok'), ok_icon) 
            ok_icon.show()            
            _activity.add_alert(self._stop_alert) 
            self._stop_alert.connect('response', self.__stop_response_cb)
            self._stop_alert.show()

            self._dl_jobject.metadata['title'] = _('File %s from %s.') % \
                                                 (file_name, self._source.spec)
            self._dl_jobject.metadata['progress'] = '100'
            self._dl_jobject.file_path = self._target_file.path

            if self._mime_type == 'application/octet-stream':
                sniffed_mime_type = mime.get_for_file(self._target_file.path)
                self._dl_jobject.metadata['mime_type'] = sniffed_mime_type

            datastore.write(self._dl_jobject,
                            transfer_ownership=True,
                            reply_handler=self._internal_save_cb,
                            error_handler=self._internal_save_error_cb,
                            timeout=360 * DBUS_PYTHON_TIMEOUT_UNITS_PER_SECOND)

    def __start_response_cb(self, alert, response_id):
        global _active_downloads
        if response_id is gtk.RESPONSE_CANCEL:
            logging.debug('Download Canceled')
            self._cancelable.cancel(NS_ERROR_FAILURE) 
            try:
                self._datastore_deleted_handler.remove()
                datastore.delete(self._object_id)
            except:
                logging.warning('Object has been deleted already')
            if self._dl_jobject is not None:
                self._cleanup_datastore_write()
            if self._stop_alert is not None:
                _activity.remove_alert(self._stop_alert)

        _activity.remove_alert(alert)        

    def __stop_response_cb(self, alert, response_id):        
        global _active_downloads 
        if response_id is gtk.RESPONSE_APPLY: 
            logging.debug('Start application with downloaded object') 
            activity.show_object_in_journal(self._object_id) 
        _activity.remove_alert(alert)
            
    def _cleanup_datastore_write(self):
        global _active_downloads        
        _active_downloads.remove(self)

        if os.path.isfile(self._dl_jobject.file_path):
            os.remove(self._dl_jobject.file_path)
        self._dl_jobject.destroy()
        self._dl_jobject = None

    def _internal_save_cb(self):
        self._cleanup_datastore_write()

    def _internal_save_error_cb(self, err):
        logging.debug("Error saving activity object to datastore: %s" % err)
        self._cleanup_datastore_write()

    def onProgressChange64(self, web_progress, request, cur_self_progress,
                           max_self_progress, cur_total_progress,
                           max_total_progress):
        path, file_name = os.path.split(self._target_file.path)
        percent = (cur_self_progress  * 100) / max_self_progress

        if (time.time() - self._last_update_time) < _MIN_TIME_UPDATE and \
           (percent - self._last_update_percent) < _MIN_PERCENT_UPDATE:
            return

        self._last_update_time = time.time()
        self._last_update_percent = percent

        if percent < 100:
            self._dl_jobject.metadata['progress'] = str(percent)
            datastore.write(self._dl_jobject)

    def _create_journal_object(self):
        path, file_name = os.path.split(self._target_file.path)

        self._dl_jobject = datastore.create()
        self._dl_jobject.metadata['title'] = _('Downloading %s from \n%s.') \
                                             %(file_name, self._source.spec)

        self._dl_jobject.metadata['progress'] = '0'
        self._dl_jobject.metadata['keep'] = '0'
        self._dl_jobject.metadata['buddies'] = ''
        self._dl_jobject.metadata['preview'] = ''
        self._dl_jobject.metadata['icon-color'] = profile.get_color().to_string()
        self._dl_jobject.metadata['mime_type'] = self._mime_type
        self._dl_jobject.file_path = ''
        datastore.write(self._dl_jobject)

        bus = dbus.SessionBus()
        obj = bus.get_object(DS_DBUS_SERVICE, DS_DBUS_PATH)
        datastore_dbus = dbus.Interface(obj, DS_DBUS_INTERFACE)
        self._datastore_deleted_handler = datastore_dbus.connect_to_signal(
            'Deleted', self.__datastore_deleted_cb,
            arg0=self._dl_jobject.object_id)

    def __datastore_deleted_cb(self, uid):
        logging.debug('Downloaded entry has been deleted from the datastore: %r' % uid)
        # TODO: Use NS_BINDING_ABORTED instead of NS_ERROR_FAILURE.
        self._cancelable.cancel(NS_ERROR_FAILURE) #NS_BINDING_ABORTED)

components.registrar.registerFactory('{23c51569-e9a1-4a92-adeb-3723db82ef7c}"',
                                     'Sugar Download',
                                     '@mozilla.org/transfer;1',
                                     Factory(Download))

