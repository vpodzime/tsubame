# -*- coding: utf-8 -*-
#----------------------------------------------------------------------------
# A Tsubame Qt 5 QtQuick 2.0 GUI module
#----------------------------------------------------------------------------
# Copyright 2017, Martin Kolman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#---------------------------------------------------------------------------
import os
import re

import pyotherside

import threading

from core import constants
from core.threads import threadMgr
from core import utils
from core import tsubame_log
from core import stream as stream_module
from gui.gui_base import GUI

import logging
no_prefix_log = logging.getLogger()
log = logging.getLogger("mod.gui.qt5")
qml_log = logging.getLogger("mod.gui.qt5.qml")

def newlines2brs(text):
    """ QML uses <br> instead of \n for linebreak """
    return re.sub('\n', '<br>', text)

class Qt5GUI(GUI):
    """A Qt 5 + QtQuick 2 GUI module."""

    def __init__(self, tsubame):
        super(Qt5GUI, self).__init__(tsubame)

        # some constants
        size = (800, 480) # initial window size
        self._screen_size = None

        # we handle notifications by forwarding them to the QML context
        self.tsubame.notification_triggered.connect(self._dispatch_notification_cb)

        # register exit handler
        #pyotherside.atexit(self._shutdown)
        # FIXME: for some reason the exit handler is never
        # called on Sailfish OS, so we use a onDestruction
        # handler on the QML side to trigger shutdown

        # window state
        self._fullscreen = False

        # get screen resolution
        # TODO: implement this
        #screenWH = self.screen_wh()
        #self.log.debug(" @ screen size: %dx%d" % screenWH)
        #if self.highDPI:
        #    self.log.debug(" @ high DPI")
        #else:
        #    self.log.debug(" @ normal DPI")

        # NOTE: what about multi-display devices ? :)

        ## add image providers

        self._imageProviders = {
            "icon" : IconImageProvider(self),
        }
        # we will like add an image provider for media attached to messages in the future

        # log what version of PyOtherSide we are using
        # - we log this without prefix as this shows up early
        #   during startup, so it looks nicer that way :-)
        no_prefix_log.info("using PyOtherSide %s", pyotherside.version)

        ## register the actual callback, that
        ## will call the appropriate provider base on
        ## image id prefix
        pyotherside.set_image_provider(self._select_image_provider_cb)

        self._notificationQueue = []

        # make the log manager easily accessible
        self.log_manager = tsubame_log.log_manager

        # stream management
        self.streams = Streams(self)

        # log for log messages from the QML context
        self.qml_log = qml_log
        # queue a notification to QML context that
        # a Python loggers is available
        pyotherside.send("loggerAvailable")

    def _shutdown(self):
        """Called by PyOtherSide once the QML side is shutdown.
        """
        self.log.info("Qt 5 GUI module shutting down")
        # save options, just in case
        self._save_options()
        # tell the main class instance
        self.tsubame.shutdown()

    @property
    def gui_id(self):
        return "qt5"

    @property
    def has_notification_support(self):
        return True

    def _dispatch_notification_cb(self, text, ms_timeout=5000):
        """Let the QML context know that it should show a notification.

        :param str text: text of the notification message
        :param int ms_timeout: how long to show the notification in ms
        """

        self.log.debug("notify:\n message: %s, timeout: %d" % (text, ms_timeout))
        pyotherside.send("pythonNotify", {
            "message" : newlines2brs(text),  # QML uses <br> in place of \n
            "timeout" : ms_timeout
        })

    def open_url(self, url):
        # TODO: implement this
        pass

    @property
    def screen_wh(self):
        return self._screen_size

    @property
    def tsubame_version(self):
        """Report current Tsubame version or "unknown" if version info is not available."""
        version = self.tsubame.paths.version_string
        if version is None:
            return "unknown"
        else:
            return version

    def _select_image_provider_cb(self, image_id, requestedSize):
        original_image_id = image_id
        provider_id = ""
        #self.log.debug("SELECT IMAGE PROVIDER")
        #self.log.debug(image_id)
        #self.log.debug(image_id.split("/", 1))
        try:
            # split out the provider id
            provider_id, image_id = image_id.split("/", 1)
            # get the provider and call its get_image()
            return self._imageProviders[provider_id].get_image(image_id, requestedSize)
        except ValueError:  # provider id missing or image ID overall wrong
            self.log.error("provider ID missing: %s", original_image_id)
        except AttributeError:  # missing provider (we are calling methods of None ;) )
            if provider_id:
                self.log.error("image provider for this ID is missing: %s", provider_id)
            else:
                self.log.error("image provider broken, image id: %s", original_image_id)
        except Exception:  # catch and report the rest
            self.log.exception("image loading failed, imageId: %s", original_image_id)

    def _get_startup_values(self):
        """ Return a dict of values needed by the Qt 5 GUI right after startup.
        
        By grouping the requested values in a single dict we reduce the number
        of Python <-> QML roundtrips and also make it possible to more easily
        get these values asynchronously (values arrive all at the same time,
        not in random order at random time).

        :returns: a dict gathering the requested values
        :rtype dict:
        """
        values = {
            "tsubame_version" : self.tsubame_version,
            "constants" : self.constants,
            "show_quit_button": self.show_quit_button,
            "fullscreen_only": self.tsubame.platform.fullscreen_only,
            "should_start_in_fullscreen": self.should_start_in_fullscreen,
            "needs_back_button": self.tsubame.platform.needs_back_button,
            "needs_page_background": self.tsubame.platform.needs_page_background,
            "sailfish" : self.tsubame.platform.platform_id == "jolla",
            "highDPI" : self.highDPI,
            "theme" : self.theme
        }
        return values

    def _set_screen_size(self, screen_size):
        """A method called by QML to report current screen size in pixels.

        :param screen_size: screen width and height in pixels
        :type screen_size: a tuple of integers
        """
        self._screen_size = screen_size

class Streams(object):
    """An easy to use interface to message streams for the QML context."""

    def __init__(self, gui):
        self.gui = gui

    def get_stream_list(self):
        """Get list of message streams."""
        stream_list = stream_module.stream_manager.stream_list
        # Populate the stream list with some initial content if empty.
        if not stream_list:
            self.gui.log.info("stream list is empty - adding initial streams")
            stream_module.stream_manager.add_initial_streams()
            stream_list = stream_module.stream_manager.stream_list

        stream_dict_list = []
        for stream in stream_list:
            # Convert the list of stream objects to a list of dicts
            # created from the underlying data objects.
            # That should work for now and we can do something more
            # sophisticated later. :)
            stream_dict_list.append(dict(stream.data))

        return stream_dict_list

    def get_stream_messages(self, stream_name, refresh=False):
        """Get a list of messages for stream identified by stream name."""
        stream = stream_module.stream_manager.stream_dict.get(stream_name, None)
        if stream:
            if refresh:
                stream.refresh()
            message_list = []

            for message in stream.messages:
                message_dict = {
                    "text" : message.full_text,
                    "username" : message.user.screen_name
                }
                message_list.append(message_dict)

            return message_list
        else:
            self.gui.log.error("Stream with this name does not exist: %s" % stream_name)
            return []


class Search(object):
    """An easy to use search interface for the QML context."""

    def __init__(self, gui):
        self.gui = gui
        self._threadsInProgress = {}
        # register the thread status changed callback
        threadMgr.threadStatusChanged.connect(self._threadStatusCB)

    def search(self, searchId, query):
        """Trigger an asynchronous search (specified by search id)
        for the given term

        :param str query: search query
        """
        online = self.gui.m.get("onlineServices", None)
        if online:
            # construct result handling callback
            callback = lambda x : self._searchCB(searchId, x)
            # get search function corresponding to the search id
            searchFunction = self._getSearchFunction(searchId)
            # start the search and remember the search thread id
            # so we can use it to track search progress
            # (there might be more searches in progress so we
            #  need to know the unique search thread id)
            threadId = searchFunction(query, callback)
            self._threadsInProgress[threadId] = searchId
            return threadId

    def _searchCB(self, searchId, results):
        """Handle address search results

        :param list results: address search results
        """
        resultList = []
        for result in results:
            resultList.append(point2dict(result))

        resultId = SEARCH_RESULT_PREFIX + searchId
        pyotherside.send(resultId, resultList)
        thisThread = threading.currentThread()
        # remove the finished thread from tracking
        if thisThread.name in self._threadsInProgress:
            del self._threadsInProgress[thisThread.name]

    def cancelSearch(self, threadId):
        """Cancel the given asynchronous search thread"""
        log.info("canceling search thread: %s", threadId)
        threadMgr.cancel_thread(threadId)
        if threadId in self._threadsInProgress:
            del self._threadsInProgress[threadId]

    def _threadStatusCB(self, threadName, threadStatus):
        # check if the event corresponds to some of the
        # in-progress search threads
        recipient = self._threadsInProgress.get(threadName)
        if recipient:
            statusId = SEARCH_STATUS_PREFIX + recipient
            pyotherside.send(statusId, threadStatus)

    def _getSearchFunction(self, searchId):
        """Return the search function object for the given searchId"""
        online = self.gui.m.get("onlineServices", None)
        if online:
            if searchId == "address":
                return online.geocodeAsync
            elif searchId == "wikipedia":
                return online.wikipediaSearchAsync
            elif searchId == "local":
                return online.localSearchAsync
            else:
                log.error("search function for id: %s not found", searchId)
                return None
        else:
            log.error("onlineServices module not found")


class ImageProvider(object):
    """PyOtherSide image provider base class"""
    def __init__(self, gui):
        self.gui = gui

    def get_image(self, imageId, requestedSize):
        pass


class IconImageProvider(ImageProvider):
    """the IconImageProvider class provides icon images to the QML layer as
    QML does not seem to handle .. in the url very well"""

    def __init__(self, gui):
        ImageProvider.__init__(self, gui)

    def get_image(self, image_id, requested_size):
        #log.debug("ICON!")
        #log.debug(image_id)
        try:
            #TODO: theme name caching ?
            theme_folder = self.gui.tsubame.paths.theme_folder_path
            # full_icon_path = os.path.join(icons_folder, image_id)
            # the path is constructed like this in QML
            # so we can safely just split it like this
            split_path = image_id.split("/")
            # remove any Ambiance specific garbage appended by Silica
            split_path[-1] = split_path[-1].rsplit("?")[0]
            theme_name = split_path[0]
            full_icon_path = os.path.join(theme_folder, *split_path)

            icon_exists = utils.internal_isfile(full_icon_path)
            if not icon_exists:
                # Not found in currently selected theme,
                # try to check the default theme.
                split_path[0] = "default"
                default_icon_path = os.path.join(theme_folder, *split_path)
                if utils.internal_isfile(default_icon_path):
                    full_icon_path = default_icon_path
                    icon_exists = True
            if not icon_exists:
                log.error("Icon not found (in both %s theme and default theme):", theme_name)
                log.error(full_icon_path)
                return None
            return utils.internal_get_file_contents(full_icon_path), (-1,-1), pyotherside.format_data
        except Exception:
            log.exception("icon image provider: loading icon failed, id:\n%s" % image_id)

class Tsubame(object):
    """Core Tsubame functionality."""

    def __init__(self, tsubame, gui):
        self.tsubame = tsubame
        self.gui = gui