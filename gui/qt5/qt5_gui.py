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
import blitzdb

import threading
import twitter
import re
import tempfile
import time

from core import constants
from core.threads import threadMgr
from core import utils
from core import tsubame_log
from core import stream as stream_module
from core import api as api_module
from core import user as user_module
from core import download
from core import account as account_module
from core import list as list_module
from core import db as db_module
from core import cache as cache_module
from core.signal import Signal
from gui.gui_base import GUI

REMOVE_HTML_RE = re.compile('<[^<]+?>')

import logging
no_prefix_log = logging.getLogger()
log = logging.getLogger("mod.gui.qt5")
qml_log = logging.getLogger("mod.gui.qt5.qml")

IMAGE_SOURCE_TWITTER = "twitter"


class TwitterAPIUsernameNotFound(Exception):

    def __init__(self, account_username):
        self.account_username = account_username

    def __str__(self):
        return "no API instance found for account username: %s" % self.account_username

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
        self._temp_stream_api_username = None

        # we handle notifications by forwarding them to the QML context
        self.tsubame.notification_triggered.connect(self._dispatch_notification_cb)

        self.shutdown = Signal()
        self.all_classes_instantiated = Signal()

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

        # handling of users
        self.users = Users(self)

        # download handling
        self.download = Download(self)

        # account handling
        self.accounts = Accounts(self)

        # list handling
        self.lists = Lists(self)

        # log for log messages from the QML context
        self.qml_log = qml_log
        # queue a notification to QML context that
        # a Python loggers is available
        pyotherside.send("loggerAvailable")

        # debugging properties
        self.debug_message_content = False

        # all modules we care for should now be instantiated
        self.all_classes_instantiated()

    def get_twitter_api(self, account_username):
        """Get API instance corresponding to the account username.

        :param str account_username: account username for the API
        :returns: Twitter api instance corresponding to the account username
        :raises: TwitterAPIUsernameNotFound
        """
        api = api_module.api_manager.get_twitter_api(account_username=account_username)
        if api is None:
            raise TwitterAPIUsernameNotFound(account_username)
        return api

    @property
    def general_purpose_twitter_api_username(self):
        # Basically one of the accounts for stuff like fetching data for temporary streams
        # or looking up user information. In most cases any valid Twitter account should do.
        # TODO: make this configurable
        if not self._temp_stream_api_username:
            self._temp_stream_api_username = api_module.api_manager.get_an_api_username()
            self.log.debug("api username for temporary streams: %s", self._temp_stream_api_username)
        return self._temp_stream_api_username

    @property
    def general_purpose_twitter_api(self):
        """Get an API for the general purpose account username."""
        return self.get_twitter_api(self.general_purpose_twitter_api_username)

    def _shutdown(self):
        """Called by PyOtherSide once the QML side is shutdown.
        """
        self.log.info("Qt 5 GUI module shutting down")
        # save options, just in case
        self._save_options()
        # trigger the shutdown signal
        self.shutdown()

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
        # send signal to the QML context to open the provided URL
        pyotherside.send("openURl", url)

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
            "device_type" : self.tsubame.platform.device_type,
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

class Download(object):
    """An easy to use interface for file download for the QML context."""

    def __init__(self, gui):
        self.gui = gui

    def download_image(self,
                       url,
                       image_source=IMAGE_SOURCE_TWITTER,
                       monthly_subfolders=True):
        filename = url.split('/')[-1]
        # TODO: use constant
        if image_source == IMAGE_SOURCE_TWITTER:
            # split the ":<stuff>" suffix that Twitter image URLs might have
            filename = filename.rsplit(":", 1)[0]
        download_folder = os.path.join(self.gui.tsubame.paths.pictures_folder_path,
                                       "tsubame",
                                       image_source)
        if monthly_subfolders:
            download_folder = os.path.join(download_folder, time.strftime("%Y_%m"))
        return download.download_file_(url=url, download_folder=download_folder, filename=filename)


class Streams(object):
    """An easy to use interface to message streams for the QML context."""

    def __init__(self, gui):
        self.gui = gui

        # connect to the shutdown signal for cleanup purposes
        self.gui.shutdown.connect(self._shutdown)

        # temporary BlitzDB instance
        self._tempdir = tempfile.TemporaryDirectory()
        db_tempfile = os.path.join(self._tempdir.name, "temp.db")
        self.gui.log.debug("creating temp db in: %s", db_tempfile)
        self._temp_db = blitzdb.FileBackend(db_tempfile)

        # prevent the stream list populate from running if this is not the initial
        # get_named_stream_list() call
        self._first_get_stream_list_run = True

        self._temporary_streams = {}
        self._temporary_stream_id = -1
        self._temporary_stream_id_lock = threading.RLock()

    def get_temporary_stream_id(self):
        """Atomically return a unique id that can be used to name a temporary stream.

        We convert the integer to a string for consistency as it will get converted
        to a string anyway on the way to QML and back.

        :return: temporary stream id
        :rtype: str
        """
        with self._temporary_stream_id_lock:
            self._temporary_stream_id += 1
            return str(self._temporary_stream_id)

    def get_named_stream_list(self):
        """Get list of message streams."""
        stream_list = stream_module.stream_manager.stream_list
        # Populate the stream list with some initial content if empty.
        if not stream_list and self._first_get_stream_list_run:
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

        self._first_get_stream_list_run = False
        return stream_dict_list

    def _process_twitter_message(self, message, active_message_id=None):
        """Turn the twitter message to dict and apply any Tsubame related tweaks."""
        message_dict = message.AsDict()
        matches_active_id = active_message_id and message_dict.get("id_str") == active_message_id

        # make normal URLs clickable
        for url in message_dict.get("urls", []):
            full_text = message_dict["full_text"]
            short_url = url["url"]
            link = '<a href="%s">%s</a>' % (url["expanded_url"], url["expanded_url"])
            message_dict["full_text"] = full_text.replace(short_url, link)

        # drop media URLs as we show a per-media detail page
        for medium in message_dict.get("media", []):
            full_text = message_dict["full_text"]
            message_dict["full_text"] = full_text.replace(medium["url"], "").rstrip()

        # make hashtags clickable
        # - we cant really do this with regexps on the QML side as the Javascript regexps
        #   can't really handle non latin characters such as Japanese
        # - if this even turns into a bottleneck we can likely move these substitutions
        #   to the delegate on the QML side
        for hashtag in message_dict.get("hashtags", []):
            full_text = message_dict["full_text"]
            hashtag_string = hashtag["text"]
            link = '<a href="#%s">#%s</a>' % (hashtag_string, hashtag_string)
            message_dict["full_text"] = full_text.replace("#" + hashtag_string, link)


        message_dict["tsubame_message_type"] = constants.MessageType.TWEET.value
        message_dict["tsubame_message_created_at_epoch"] = message.created_at_in_seconds
        message_dict["tsubame_message_source_plaintext"] = REMOVE_HTML_RE.sub("", message.source)
        return message_dict, matches_active_id

    def get_stream_messages(self, stream_name, temporary=False):
        """Get a list of messages for stream identified by stream name."""
        if temporary:
            stream = self._temporary_streams.get(stream_name)
        else:
            stream = stream_module.stream_manager.stream_dict.get(stream_name, None)
        if stream:
            message_list = []
            active_message_id = None
            match_index = None
            if stream.active_message_id:
                stream_type = stream.active_message_id.split("_")[0]
                if stream_type == constants.MessageType.TWEET.value:
                    active_message_id = stream.active_message_id.split("_")[1]
            for message in stream.messages:
                if isinstance(message, twitter.Status):
                    message_dict, match = self._process_twitter_message(message, active_message_id)
                    message_list.append(message_dict)
                    if match:
                        match_index = len(message_list)-1
                    if self.gui.debug_message_content:
                        log.debug("MESSAGE:")
                        log.debug(message)
                else:
                    self.gui.log.error("skipping unsupported message from stream %s: %s", stream, message)
            return [message_list, match_index]
        else:
            self.gui.log.error("Stream with this name does not exist: %s" % stream_name)
            return [[], None]

    def get_hashtag_stream(self, hashtag):
        """ Return a temporary hashtag stream id.

        The id can be used to retrieve stream messages and to
        remove the stream once it is no longer needed.

        :param str hashtag: a Twitter hashtag
        :return: id of a temporary hashtag stream
        """

        # create temporary stream
        hashtag_stream = stream_module.MessageStream.new(
            db = self._temp_db,
            name = "#%s" % hashtag
        )
        # create temporary source
        hashtag_stream_source = stream_module.TwitterHashtagTweets.new(
            db = self._temp_db,
            api_username=self.gui.general_purpose_twitter_api_username,
            hashtag = hashtag
        )
        hashtag_stream_source.cache_messages = False
        hashtag_stream.inputs.add(hashtag_stream_source)
        hashtag_stream.refresh()
        return self._store_temporary_stream(hashtag_stream)

    def get_user_tweets_stream(self, username):
        """ Return a temporary user tweet stream id.

        The id can be used to retrieve stream messages and to
        remove the stream once it is no longer needed.

        :param str username: a Twitter username
        :return: id of a temporary user message stream
        """

        # create temporary stream
        user_tweet_stream = stream_module.MessageStream.new(
            db = self._temp_db,
            name = "@%s tweets" % username
        )
        # create temporary source
        user_tweet_stream_source = stream_module.TwitterUserTweets.new(
            db = self._temp_db,
            api_username=self.gui.general_purpose_twitter_api_username,
            source_username = username
        )
        user_tweet_stream_source.cache_messages = False
        user_tweet_stream.inputs.add(user_tweet_stream_source)
        user_tweet_stream.refresh()
        return self._store_temporary_stream(user_tweet_stream)

    def get_user_favorites_stream(self, username):
        """ Return a temporary user favorites stream id.

        The id can be used to retrieve stream messages and to
        remove the stream once it is no longer needed.

        :param str username: a Twitter username
        :return: id of a temporary user favorites stream
        """

        # create temporary stream
        user_favorites_stream = stream_module.MessageStream.new(
            db = self._temp_db,
            name = "@%s favorites" % username
        )
        # create temporary source
        user_favorites_stream_source = stream_module.TwitterUserFavorites.new(
            db = self._temp_db,
            api_username=self.gui.general_purpose_twitter_api_username,
            source_username = username
        )
        user_favorites_stream_source.cache_messages = False
        user_favorites_stream.inputs.add(user_favorites_stream_source)
        user_favorites_stream.refresh()
        return self._store_temporary_stream(user_favorites_stream)

    def get_list_stream(self, account_username, list_owner_username, list_slug):
        """ Return a temporary list stream id.

        The id can be used to retrieve stream messages and to
        remove the stream once it is no longer needed.

        :param account_username: known account username or none to use general purpose account/API
        :type account_username: str or None
        :param str list_owner_username: username of the list owner
        :param str list_slug: safe name of the list
        :return: id of a temporary list stream
        """
        log.debug("creating temp stream: api:%s @%s/%s", account_username, list_owner_username, list_slug)

        log.debug("API USERNAME: %s", account_username)

        if account_username is None:
            api = self.gui.general_purpose_twitter_api
        else:
            api = self.gui.get_twitter_api(account_username)

        # create temporary stream
        list_stream = stream_module.MessageStream.new(
            db = self._temp_db,
            name = "%s@%s/%s" % (account_username, list_owner_username, list_slug)
        )
        # create temporary source
        list_stream_source = stream_module.TwitterRemoteList.new_from_name(
            db = self._temp_db,
            api=api,
            list_owner_username=list_owner_username,
            list_name=list_slug
        )
        list_stream_source.cache_messages = False
        list_stream.inputs.add(list_stream_source)
        list_stream.refresh()
        return self._store_temporary_stream(list_stream)

    def _store_temporary_stream(self, stream):
        temporary_stream_id = self.get_temporary_stream_id()
        self._temporary_streams[temporary_stream_id] = stream
        return temporary_stream_id

    def refresh_stream(self, stream_name, temporary=False):
        """Get a message stream identified by stream name."""
        if temporary:
            stream = self._temporary_streams.get(stream_name)
        else:
            stream = stream_module.stream_manager.stream_dict.get(stream_name, None)

        if stream:
            message_list = []
            new_messages = stream.refresh()
            for message in new_messages:
                if isinstance(message, twitter.Status):
                    message_dict, _match = self._process_twitter_message(message)
                    message_list.append(message_dict)
                else:
                    self.gui.log.error("skipping unsupported message from stream %s: %s", stream, message)
            return message_list
        else:
            self.gui.log.error("Can't refresh stream.")
            self.gui.log.error("Stream with this name does not exist: %s" % stream_name)
            return []

    def delete_stream(self, stream_name):
        """Try to delete a stream by name."""
        return stream_module.stream_manager.delete_stream(stream_name)

    def remove_temporary_stream(self, stream_name):
        if stream_name in self._temporary_streams:
            self.gui.log.debug("removing temp stream: %s", stream_name)
            del self._temporary_streams[stream_name]

    def set_stream_active_message(self, stream_name, message_data):
        """Set active message id for a stream."""
        message_type = message_data.get("tsubame_message_type")
        if message_type == constants.MessageType.TWEET.value:
            message_id = message_data["id_str"]
            stream = stream_module.stream_manager.stream_dict.get(stream_name, None)
            if stream:
                stream.active_message_id = "%s_%s" % (message_type, message_id)
                stream.save(commit=True)
            else:
                self.gui.log.error("Can't set active message id for stream.")
                self.gui.log.error("Stream with this name does not exist: %s" % stream_name)
                return []
        else:
            self.gui.log.error("Can't set active message id - unknown message type: %s", message_type)

    def _shutdown(self):
        """A general purpose shutdown method."""
        self._tempdir.cleanup()

class Users(object):
    """Twitter user handling."""

    def __init__(self, gui):
        self.gui = gui

    def get_user_info(self, username):
        """Get information about a user (if available).

        :param str username: username of user to lookup

        :returns: information about the user (if any)
        :rtype: dict

        """
        api = self.gui.general_purpose_twitter_api
        result = user_module.get_user_info(api, username)
        if result:
            result_dict = result.AsDict()
            return result_dict
        else:
            return None

    def get_user_lists(self, username):
        """Get public lists of the given user.

        :param str username: username of user to lookup
        :returns: dict with information about public lists owned by the user
        """
        api = self.gui.general_purpose_twitter_api
        public_lists = [l for l in list_module.get_users_lists(api, username) if l.mode == list_module.TWITTER_LIST_MODE_PUBLIC]
        return {
            "public_lists" : [l.AsDict() for l in public_lists],
            "public_list_count" : len(public_lists)
        }

class Accounts(object):
    """Twitter account handling."""

    def __init__(self, gui):
        self.gui = gui
        self._account_caches = {}
        self.gui.all_classes_instantiated.connect(self._connect_signals)

    def _connect_signals(self):
        self.gui.lists.new_list_created.connect(self._on_list_created)

    def _on_list_created(self, account_username, private, list_data):
        """Callback for the new_list_created signal of the Lists class.

        This updates our account user info cache for the given account."""
        cache = self._get_account_cache(account_username)
        if private:
            cache.add_lists(private_lists=[list_data], public_lists=[])
        else:
            cache.add_lists(private_lists=[], public_lists=[list_data])
        cache.save(commit=True)

    def _get_account_cache(self, account_username):
        """Return cache object corresponding to the account username.

        :param str account_username: an account username

        NOTE: The cache object can either contain account user data
              or it might be a freshly created cache object without any data.
        """

        # first check the local dict
        cache = self._account_caches.get(account_username)
        # not yet referenced in local dict
        if not cache:
            cache_db = db_module.db_manager.tweet_cache
            try:
                cache = cache_module.AccountInfoCache.from_db(db=cache_db, account_username=account_username)
            except blitzdb.Document.DoesNotExist:
                log.debug("creating account user info cache for %s", account_username)
                cache = cache_module.AccountInfoCache.new(db=cache_db, account_username=account_username)
            self._account_caches[account_username] = cache

        return cache

    def get_account(self, account_username):
        """Return an added account by username (if present).

        :param str account_username: account username
        :returns: dict describing the account or None when account has not been found
        :rtype: dict or None
        """
        return account_module.account_manager.twitter_accounts.get(account_username)

    def check_account_list_membership(self, account_username, username):
        """Check if a user is member of some lists owned by the given account.

        Return a list of all lists owned by the account and mark lists of which
        the user is a member.

        :param str account_username: account username
        :param str username: username for membership test
        :return: marked up private lists and marked up public lists
        """
        api = self.gui.get_twitter_api(account_username=account_username)
        private_lists, public_lists = self.get_lists_owned_by_account(account_username)
        list_membership = list_module.get_list_membership(api, username)
        membership_set = set([l.slug for l in list_membership])
        private_lists_with_membership = []
        public_lists_with_membership = []

        for l in private_lists:
            d = {
                "list_info" : l,
                "is_member" : l["slug"] in membership_set
            }
            private_lists_with_membership.append(d)

        for l in public_lists:
            d = {
                "list_info" : l,
                "is_member" : l["slug"] in membership_set
            }
            public_lists_with_membership.append(d)

        return private_lists_with_membership, public_lists_with_membership

    def _fetch_lists_owned_by_account(self, account_username):
        """Fetch information about lists "owned" by an account added to Tsubame from the API.

        Private and public lists will be returned as two separate lists.

        :param str account_username: account username to the the lists for
        :returns: dict with information about the lists owned by the account
        """
        account_private_lists = []
        account_public_lists = []

        api = self.gui.get_twitter_api(account_username=account_username)
        try:
            lists = list_module.get_lists(api)
        except Exception:
            # this will most likely be due to a rate limit being hit
            log.exception("can't get lists owned by account %s", account_username)
            lists = []
        # Filter lists to public and private.Also convert the list objects
        # to dicts for QML to consume when we are at it.
        for l in lists:
            if l.mode == "private":
                account_private_lists.append(l)
            elif l.mode == "public":
                account_public_lists.append(l)
            else:
                log.error("get_lists_owned_by_account(): unknown list mode %s, skipping list %s", l.mode, l.name)

        return account_private_lists, account_public_lists


    def get_account_user_info(self, account_username):
        """Get information about Twitter user corresponding to the account.

        NOTE: this method takes care of caching the account user info
              and only queries the API if the cache is outdated or not
              present

        :returns: information about the user
        :rtype: dict
        """
        return self._get_account_cache(account_username).user_info

    def _get_cache_for_account(self, account_username):
        """Return the cached account user info & make sure it is up to date."""

        # check if we have valid user info cache for this account username
        cache = self._get_account_cache(account_username)

        if cache.valid:
            log.debug("returning account user info for %s from cache", account_username)
        else :  # fetch new info from Twitter API and update the cache before returning user info
            log.debug("refreshing account user info cache for %s", account_username)
            # dump any cache
            cache.clear()
            # get fresh user info
            api = self.gui.get_twitter_api(account_username)
            user_info = user_module.get_user_info(api, account_username).AsDict()
            # get fresh list info
            private_lists, public_lists = self._fetch_lists_owned_by_account(account_username)
            # cache it
            cache.user_info = user_info
            cache.add_lists(private_lists=private_lists, public_lists=public_lists)
            # save the cache
            cache.user_info = user_info
            cache.save(commit=True)
        return cache

    def get_lists_owned_by_account(self, account_username):
        """Return private and public lists owned by the account.

        :param str account_username: account username
        :returns: list of private lists and list of public lists
        """
        cache = self._get_cache_for_account(account_username)
        return cache.private_lists, cache.public_lists

    def get_account_list(self):
        """List all Twitter accounts that have been added to Tsubame.

        :return: list of dicts representing all added accounts.
        :rtype: list of dicts
        """
        accounts = []
        for account in account_module.account_manager.twitter_accounts.values():
            account_dict = {"username" : account.username,
                            "name" : account.name}
            accounts.append(account_dict)
        self.gui.log.debug("%d accounts are available" % len(accounts))
        return accounts

    def add_account(self, account_username,
                    access_token_key,
                    access_token_secret,
                    account_name=""):
        """Add an account.

        :param str account_username: account username
        :param str access_token_key: access token key
        :param str access_token_secret: access token secret
        :param str account_name: optional account name
        """
        # we don't want zero length account names
        if not account_name:
            account_name = account_username

        account_module.account_manager.add()
        new_account = account_module.TwitterAccount.new(
            db=self.gui.tsubame.db.main,
            username=account_username,
            name=account_name,
            token=access_token_key,
            token_secret=access_token_secret
        )
        account_module.account_manager.add(account=new_account)

    def remove_account(self, account_username):
        """Remove an account.

        :param str account_username: account specified by username
        """
        account_module.account_manager.remove(account_username=account_username)

class Lists(object):
    """Twitter list handling."""

    def __init__(self, gui):
        self.gui = gui
        self.new_list_created = Signal()

    def create_new_list(self, account_username, list_name, description, private):
        """Create a new list.

        :param str account_username: account username for API access
        :param str list_name: name of the new list
        :param str description: optional list description
        :param bool private: if the newly created list should be private or public
        """
        # if no valid description is empty, indicate that to the API
        if not description:
            description = None
        api = self.gui.get_twitter_api(account_username)
        result = list_module.create_list(api=api, list_name=list_name,
                                description=description, private=private)
        # trigger a signal that a new list has been created
        self.new_list_created(account_username, private, result)


    def destroy_list(self, account_username, list_owner_username, list_name):
        """Remove a list.

        :param str account_username: account username for API access
        :param str list_owner_username: list owner of the new list
        :param str list_name: name of the new list
        """
        # if no valid description is empty, indicate that to the API
        api = self.gui.get_twitter_api(account_username)
        list_module.destroy_list(api=api,
                                 list_owner_username=list_owner_username,
                                 list_name=list_name)

    def add_user_to_list(self, account_username, list_name, username):
        """Add a user to a list owned by an account.

        :param str account_username: account username for API access
        :param str list_name: name of the new list
        :param str username: username of the user to add
        """
        api = self.gui.get_twitter_api(account_username)
        list_module.add_user_to_list(api=api,
                                     list_owner_username=account_username,
                                     list_name=list_name,
                                     username=username)

    def remove_user_from_list(self, account_username, list_name, username):
        """Remove user from a list owned by an account.

        :param str account_username: account username for API access
        :param str list_name: name of the new list
        :param str username: username of the user to remove from the list
        """
        api = self.gui.get_twitter_api(account_username)
        list_module.remove_user_from_list(api=api,
                                          list_owner_username=account_username,
                                          list_name=list_name,
                                          username=username)


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