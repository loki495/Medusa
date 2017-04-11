# coding=utf-8
# Author: p0psicles
#
# This file is part of Medusa.
#
# Medusa is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Medusa is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Medusa. If not, see <http://www.gnu.org/licenses/>.
"""TVDB2 api module."""
import datetime
import logging
from collections import OrderedDict

from medusa import ui
from medusa.app import FALLBACK_PLEX_API_URL

from requests.compat import urljoin
from requests.exceptions import RequestException

from tvdbapiv2 import (ApiClient, SearchApi, SeriesApi, UpdatesApi)
from tvdbapiv2.auth.tvdb import TVDBAuth
from tvdbapiv2.exceptions import ApiException

from medusa import ui
from medusa.app import FALLBACK_PLEX_API_URL, TVDB_API_KEY

from ..indexer_base import (Actor, Actors, BaseIndexer)
from ..indexer_exceptions import (IndexerAuthFailed, IndexerError, IndexerException, IndexerShowIncomplete,
                                  IndexerShowNotFound, IndexerShowNotFoundInLanguage, IndexerUnavailable)
from ..indexer_ui import BaseUI, ConsoleUI

logger = logging.getLogger(__name__)

API_BASE_TVDB = 'https://api.thetvdb.com'
API_BASE_URL_FALLBACK = FALLBACK_PLEX_API_URL


def plex_fallback(func):
    """Fallback to plex if tvdb fails to connect.

    Decorator that can be used to catch an exception and fallback to the plex proxy.
    If there are issues with tvdb the plex mirror will also only work for a limited amount of time. That is why we
    revert back to tvdb after an x amount of hours.
    """
    def inner(*args, **kwargs):
        session = args[0].config['session']
        fallback_config = session.fallback_config

        if not session.fallback_config['fallback_plex_enable']:
            return func(*args, **kwargs)

        def fallback_notification():
            ui.notifications.error('Tvdb2.plex.tv fallback',
                                   'You are currently using the tvdb2.plex.tx fallback, '
                                   'as tvdb source. Moving back to thetvdb.com in {time_left} minutes.'
                                   .format(
                                       time_left=divmod(((fallback_config['plex_fallback_time'] +
                                                          datetime.timedelta(hours=fallback_config['fallback_plex_timeout'])) -
                                                         datetime.datetime.now()).total_seconds(), 60)[0]
                                   ))

        # Check if we need to revert to tvdb's api, because we exceed the fallback period.
        if fallback_config['api_base_url'] == API_BASE_URL_FALLBACK:
            if fallback_config['fallback_plex_notifications']:
                fallback_notification()
            if (fallback_config['plex_fallback_time'] +
                    datetime.timedelta(hours=fallback_config['fallback_plex_timeout']) < datetime.datetime.now()):
                session.api_client.host = API_BASE_TVDB
                session.auth = TVDBAuth(api_key=TVDB_API_KEY)
            else:
                logger.debug("Plex fallback still enabled.")

        try:
            # Run api request
            return func(*args, **kwargs)
        except ApiException as e:
            logger.warning("could not connect to TheTvdb.com, reason '%s'", e.reason)
        except IndexerUnavailable as e:
            logger.warning("could not connect to TheTvdb.com, with reason '%s'", e.message)
        except Exception as e:
            logger.warning("could not connect to TheTvdb.com, with reason '%s'", e.message)

        # If we got this far, it means we hit an exception, and we want to switch to the plex fallback.
        session.api_client.host = API_BASE_URL_FALLBACK
        session.auth = TVDBAuth(api_key=TVDB_API_KEY, api_base=API_BASE_URL_FALLBACK)

        fallback_config['plex_fallback_time'] = datetime.datetime.now()

        # Send notification back to user.
        fallback_notification()
        # Run api request
        return func(*args, **kwargs)
    return inner


class TVDBv2(BaseIndexer):
    """Create easy-to-use interface to name of season/episode name.

    >>> indexer_api = tvdbv2()
    >>> indexer_api['Scrubs'][1][24]['episodename']
    u'My Last Day'
    """

    def __init__(self, *args, **kwargs):  # pylint: disable=too-many-locals,too-many-arguments
        """Init object."""
        super(TVDBv2, self).__init__(*args, **kwargs)

        self.indexer = 1

        self.config['base_url'] = 'http://thetvdb.com'
        self.config['api_base_url'] = API_BASE_TVDB

        # Configure artwork prefix url
        self.config['artwork_prefix'] = '%(base_url)s/banners/%%s' % self.config
        # Old: self.config['url_artworkPrefix'] = self.config['artwork_prefix']

        # client_id = ''  # (optional! Only required for the /user routes)
        # client_secret = ''  # (optional! Only required for the /user routes)


        # TODO: This can be removed when we always have one TVDB indexer object for entire medusa.
        # Currently only the session object is a singleton.
        if not hasattr(self.config['session'], 'fallback_config'):
            self.config['session'].fallback_config = {
                'plex_fallback_time': datetime.datetime.now(),
                'api_base_url': API_BASE_TVDB,
                'fallback_plex_enable': kwargs['plex_fallback']['fallback_plex_enable'],
                'fallback_plex_timeout': kwargs['plex_fallback']['fallback_plex_timeout'],
                'fallback_plex_notifications': kwargs['plex_fallback']['fallback_plex_notifications']
            }
        else:
            # Try to update some of the values
            self.config['session'].fallback_config['fallback_plex_enable'] = kwargs['plex_fallback']['fallback_plex_enable']
            self.config['session'].fallback_config['fallback_plex_timeout'] = kwargs['plex_fallback']['fallback_plex_timeout']
            self.config['session'].fallback_config['fallback_plex_notifications'] = kwargs['plex_fallback']['fallback_plex_notifications']

        if not hasattr(self.config['session'], 'api_client'):
            tvdb_client = ApiClient(self.config['api_base_url'], session=self.config['session'], api_key=TVDB_API_KEY)
            self.config['session'].api_client = tvdb_client
            self.config['session'].search_api = SearchApi(tvdb_client)
            self.config['session'].series_api = SeriesApi(tvdb_client)
            self.config['session'].updates_api = UpdatesApi(tvdb_client)

        self.config['session'].verify = False

        # An api to indexer series/episode object mapping
        self.series_map = {
            'id': 'id',
            'series_name': 'seriesname',
            'summary': 'overview',
            'first_aired': 'firstaired',
            'banner': 'banner',
            'url': 'show_url',
            'epnum': 'absolute_number',
            'episode_name': 'episodename',
            'aired_episode_number': 'episodenumber',
            'aired_season': 'seasonnumber',
            'dvd_episode_number': 'dvd_episodenumber',
            'airs_day_of_week': 'airs_dayofweek',
            'last_updated': 'lastupdated',
            'network_id': 'networkid',
            'rating': 'contentrating',
            'imdbId': 'imdb_id'
        }

    def _object_to_dict(self, tvdb_response, key_mapping=None, list_separator='|'):
        parsed_response = []

        tvdb_response = getattr(tvdb_response, 'data', tvdb_response)

        if not isinstance(tvdb_response, list):
            tvdb_response = [tvdb_response]

        for parse_object in tvdb_response:
            return_dict = {}
            if parse_object.attribute_map:
                for attribute in parse_object.attribute_map:
                    try:
                        value = getattr(parse_object, attribute, None)
                        if value is None or value == []:
                            continue

                        if isinstance(value, list):
                            if list_separator and all(isinstance(x, (str, unicode)) for x in value):
                                value = list_separator.join(value)
                            else:
                                value = [self._object_to_dict(x, key_mapping) for x in value]

                        if key_mapping and key_mapping.get(attribute):
                            if isinstance(value, dict) and isinstance(key_mapping[attribute], dict):
                                # Let's map the children, i'm only going 1 deep, because usecases that I need it for,
                                # I don't need to go any further
                                for k, v in value.iteritems():
                                    if key_mapping.get(attribute)[k]:
                                        return_dict[key_mapping[attribute][k]] = v

                            else:
                                if key_mapping.get(attribute):
                                    return_dict[key_mapping[attribute]] = value
                        else:
                            return_dict[attribute] = value

                    except Exception as e:
                        logger.warning('Exception trying to parse attribute: %s, with exception: %s', attribute,
                                       e.message)
                parsed_response.append(return_dict)
            else:
                logger.debug('Missing attribute map, cant parse to dict')

        return parsed_response if len(parsed_response) != 1 else parsed_response[0]

    def _show_search(self, show, request_language='en'):
        """Use the pytvdbv2 API to search for a show.

        @param show: The show name that's searched for as a string
        @return: A list of Show objects.
        """
        try:
            results = self.config['session'].search_api.search_series_get(name=show, accept_language=request_language)
        except ApiException as e:
            if e.status == 401:
                raise IndexerAuthFailed(
                    'Authentication failed, possible bad api key. reason: {reason} ({status})'
                    .format(reason=e.reason, status=e.status)
                )
            raise IndexerShowNotFound(
                'Show search failed in getting a result with reason: %s' % e.reason
            )
        except RequestException as e:
            raise IndexerException('Show search failed in getting a result with error: %s' % e.message)

        if results:
            return results
        else:
            return OrderedDict({'data': None})

    # Tvdb implementation
    @plex_fallback
    def search(self, series):
        """Search tvdbv2.com for the series name.

        :param series: the query for the series name
        :return: An ordered dict with the show searched for. In the format of OrderedDict{"series": [list of shows]}
        """
        series = series.encode('utf-8')
        logger.debug('Searching for show %s', [series])

        results = self._show_search(series, request_language=self.config['language'])

        if not results:
            return

        mapped_results = self._object_to_dict(results, self.series_map, '|')
        mapped_results = [mapped_results] if not isinstance(mapped_results, list) else mapped_results

        # Remove results with an empty series_name.
        # Skip shows when they do not have a series_name in the searched language. example: '24 h berlin' in 'en'
        cleaned_results = [show for show in mapped_results if show.get('seriesname')]

        return OrderedDict({'series': cleaned_results})['series']

    @plex_fallback
    def _get_show_by_id(self, tvdbv2_id, request_language='en'):  # pylint: disable=unused-argument
        """Retrieve tvdbv2 show information by tvdbv2 id, or if no tvdbv2 id provided by passed external id.

        :param tvdbv2_id: The shows tvdbv2 id
        :return: An ordered dict with the show searched for.
        """
        results = None
        if tvdbv2_id:
            logger.debug('Getting all show data for %s', [tvdbv2_id])
            try:
                results = self.config['session'].series_api.series_id_get(tvdbv2_id, accept_language=request_language)
            except ApiException as e:
                if e.status == 401:
                    raise IndexerAuthFailed(
                        'Authentication failed, possible bad api key. reason: {reason} ({status})'
                        .format(reason=e.reason, status=e.status)
                    )
                raise IndexerShowNotFound(
                    'Show search failed in getting a result with reason: {reason} ({status})'
                    .format(reason=e.reason, status=e.status)
                )
            except RequestException as e:
                raise IndexerException('Show search failed in getting a result with error: %r' % e)

        if not results:
            return

        if not getattr(results.data, 'series_name', None):
            raise IndexerShowNotFoundInLanguage('Missing attribute series_name, cant index in language: {0}'
                                                .format(request_language), request_language)

        mapped_results = self._object_to_dict(results, self.series_map, '|')

        return OrderedDict({'series': mapped_results})

    def _get_episodes(self, tvdb_id, specials=False, aired_season=None):  # pylint: disable=unused-argument
        """Get all the episodes for a show by tvdbv2 id.

        :param tvdb_id: Series tvdbv2 id.
        :return: An ordered dict with the show searched for. In the format of OrderedDict{"episode": [list of episodes]}
        """
        episodes = self._download_episodes(tvdb_id, specials=False, aired_season=None)
        return self._parse_episodes(tvdb_id, episodes)

    @plex_fallback
    def _download_episodes(self, tvdb_id, specials=False, aired_season=None):
        """Download episodes for a given tvdb_id.

        :param tvdb_id: tvdb id.
        :param specials: enable/disable download of specials. Currently not used.
        :param limit the episodes returned for a specific season.
        :return: An ordered dict of {'episode': [list of episode dicts]}
        """
        results = []
        if aired_season:
            aired_season = [aired_season] if not isinstance(aired_season, list) else aired_season

        # Parse episode data
        logger.debug('Getting all episodes of %s', [tvdb_id])

        # get paginated pages
        page = 1
        last = 1
        try:
            if aired_season:
                for season in aired_season:
                    page = 1
                    last = 1
                    while page <= last:
                        paged_episodes = self.config['session'].series_api.series_id_episodes_query_get(
                            tvdb_id, page=page, aired_season=season, accept_language=self.config['language']
                        )
                        results += paged_episodes.data
                        last = paged_episodes.links.last
                        page += 1
            else:
                while page <= last:
                    paged_episodes = self.config['session'].series_api.series_id_episodes_query_get(
                        tvdb_id, page=page, accept_language=self.config['language']
                    )
                    results += paged_episodes.data
                    last = paged_episodes.links.last
                    page += 1
        except ApiException as e:
            logger.debug('Error trying to index the episodes')
            if e.status == 401:
                raise IndexerAuthFailed(
                    'Authentication failed, possible bad api key. reason: {reason} ({status})'
                    .format(reason=e.reason, status=e.status)
                )
            raise IndexerShowIncomplete(
                'Show episode search exception, '
                'could not get any episodes. Did a {search_type} search. Exception: {e}'.format
                (search_type='full' if not aired_season else 'season {season}'.format(season=aired_season), e=e.message)
            )
        except RequestException as e:
            raise IndexerUnavailable('Error connecting to Tvdb api. Caused by: {e}'.format(e=e.message))

        if not results:
            logger.debug('Series results incomplete')
            raise IndexerShowIncomplete(
                'Show episode search returned incomplete results, '
                'could not get any episodes. Did a {search_type} search.'.format
                (search_type='full' if not aired_season else 'season {season}'.format(season=aired_season))
            )

        mapped_episodes = self._object_to_dict(results, self.series_map, '|')
        return OrderedDict({'episode': mapped_episodes if isinstance(mapped_episodes, list) else [mapped_episodes]})

    def _parse_episodes(self, tvdb_id, episode_data):
        """Parse retreived episodes."""
        if 'episode' not in episode_data:
            return False

        episodes = episode_data['episode']
        if not isinstance(episodes, list):
            episodes = [episodes]

        for cur_ep in episodes:
            if self.config['dvdorder']:
                logger.debug('Using DVD ordering.')
                use_dvd = cur_ep.get('dvd_season') is not None and cur_ep.get('dvd_episodenumber') is not None
            else:
                use_dvd = False

            if use_dvd:
                seasnum, epno = cur_ep.get('dvd_season'), cur_ep.get('dvd_episodenumber')
                if self.config['dvdorder']:
                    logger.warning("Episode doesn't have DVD order available (season: %s, episode: %s). "
                                   'Falling back to non-DVD order. '
                                   'Please consider disabling DVD order for the show with TMDB ID: %s',
                                   seasnum, epno, tvdb_id)
            else:
                seasnum, epno = cur_ep.get('seasonnumber'), cur_ep.get('episodenumber')

            if seasnum is None or epno is None:
                logger.warning('This episode has incomplete information. The season or episode number '
                               '(season: %s, episode: %s) is missing. '
                               'to get rid of this warning, you will have to contact tvdb through their forums '
                               'and have them fix the specific episode.',
                               seasnum, epno)
                continue  # Skip to next episode

            # float() is because https://github.com/dbr/tvnamer/issues/95 - should probably be fixed in TVDB data
            seas_no = int(float(seasnum))
            ep_no = int(float(epno))

            for k, v in cur_ep.items():
                k = k.lower()

                if v is not None:
                    if k == 'filename':
                        v = urljoin(self.config['artwork_prefix'], v)
                    else:
                        v = self._clean_data(v)

                self._set_item(tvdb_id, seas_no, ep_no, k, v)

    def _get_series(self, series):
        """Search thetvdb.com for the series name.

        If a custom_ui UI is configured, it uses this to select the correct
        series. If not, and interactive == True, ConsoleUI is used, if not
        BaseUI is used to select the first result.

        :param series: the query for the series name
        :return: A list of series mapped to a UI (for example: a BaseUi or custom_ui).
        """
        all_series = self.search(series)
        if not all_series:
            logger.debug('Series result returned zero')
            IndexerShowNotFound('Show search returned zero results (cannot find show on TVDB)')

        if not isinstance(all_series, list):
            all_series = [all_series]

        if self.config['custom_ui'] is not None:
            logger.debug('Using custom UI %s', [repr(self.config['custom_ui'])])
            custom_ui = self.config['custom_ui']
            ui = custom_ui(config=self.config)
        else:
            if not self.config['interactive']:
                logger.debug('Auto-selecting first search result using BaseUI')
                ui = BaseUI(config=self.config)
            else:
                logger.debug('Interactively selecting show using ConsoleUI')
                ui = ConsoleUI(config=self.config)  # pylint: disable=redefined-variable-type

        return ui.select_series(all_series)

    @plex_fallback
    def _parse_images(self, sid):
        """Parse images XML.

        From http://thetvdb.com/api/[APIKEY]/series/[SERIES ID]/banners.xml
        images are retrieved using t['show name]['_banners'], for example:

        >>> indexer_api = Tvdb(images = True)
        >>> indexer_api['scrubs']['_banners'].keys()
        ['fanart', 'poster', 'series', 'season', 'seasonwide']
        For a Poster
        >>> t['scrubs']['_banners']['poster']['680x1000']['35308']['_bannerpath']
        u'http://thetvdb.com/banners/posters/76156-2.jpg'
        For a season poster or season banner (seasonwide)
        >>> t['scrubs']['_banners']['seasonwide'][4]['680x1000']['35308']['_bannerpath']
        u'http://thetvdb.com/banners/posters/76156-4-2.jpg'
        >>>

        Any key starting with an underscore has been processed (not the raw
        data from the XML)

        This interface will be improved in future versions.
        """
        key_mapping = {'file_name': 'bannerpath', 'language_id': 'language', 'key_type': 'bannertype',
                       'resolution': 'bannertype2', 'ratings_info': {'count': 'ratingcount', 'average': 'rating'},
                       'thumbnail': 'thumbnailpath', 'sub_key': 'sub_key', 'id': 'id'}

        search_for_image_type = self.config['image_type']

        logger.debug('Getting show banners for %s', sid)
        _images = {}

        # Let's get the different types of images available for this series
        try:
            series_images_count = self.config['session'].series_api.series_id_images_get(
                sid, accept_language=self.config['language']
            )
        except (ApiException, RequestException) as e:
            logger.info('Could not get image count for showid: %s with reason: %r', sid, e.message)
            return

        for image_type, image_count in self._object_to_dict(series_images_count).items():
            try:
                if search_for_image_type and search_for_image_type != image_type:
                    # We want to use the 'poster' image also for the 'poster_thumb' type
                    if image_type != 'poster' or image_type == 'poster' and search_for_image_type != 'poster_thumb':
                        continue

                if not image_count:
                    continue

                if image_type not in _images:
                    _images[image_type] = {}

                images = self.config['session'].series_api.series_id_images_query_get(
                    sid, key_type=image_type, accept_language=self.config['language']
                )
                for image in images.data:
                    # Store the images for each resolution available
                    # Always provide a resolution or 'original'.
                    resolution = image.resolution or 'original'
                    if resolution not in _images[image_type]:
                        _images[image_type][resolution] = {}

                    # _images[image_type][resolution][image.id] = image_dict
                    image_attributes = self._object_to_dict(image, key_mapping)

                    bid = image_attributes.pop('id')

                    if image_type in ['season', 'seasonwide']:
                        if int(image.sub_key) not in _images[image_type][resolution]:
                            _images[image_type][resolution][int(image.sub_key)] = {}
                        if bid not in _images[image_type][resolution][int(image.sub_key)]:
                            _images[image_type][resolution][int(image.sub_key)][bid] = {}
                        base_path = _images[image_type][resolution][int(image.sub_key)][bid]
                    else:
                        if bid not in _images[image_type][resolution]:
                            _images[image_type][resolution][bid] = {}
                        base_path = _images[image_type][resolution][bid]

                    for k, v in image_attributes.items():
                        if k is None or v is None:
                            continue

                        if k.endswith('path'):
                            k = '_%s' % k
                            logger.debug('Adding base url for image: %s', v)
                            v = self.config['artwork_prefix'] % v

                        base_path[k] = v
            except (ApiException, RequestException) as e:
                logger.warning('Could not parse Poster for showid: %s, with exception: %s', sid, e.message)
                return

        self._save_images(sid, _images)
        self._set_show_data(sid, '_banners', _images)

    @plex_fallback
    def _parse_actors(self, sid):
        """Parser actors XML.

        From http://thetvdb.com/api/[APIKEY]/series/[SERIES ID]/actors.xml
        Actors are retrieved using t['show name]['_actors'], for example:

        >>> indexer_api = Tvdb(actors = True)
        >>> actors = indexer_api['scrubs']['_actors']
        >>> type(actors)
        <class 'tvdb_api.Actors'>
        >>> type(actors[0])
        <class 'tvdb_api.Actor'>
        >>> actors[0]
        <Actor "Zach Braff">
        >>> sorted(actors[0].keys())
        ['id', 'image', 'name', 'role', 'sortorder']
        >>> actors[0]['name']
        u'Zach Braff'
        >>> actors[0]['image']
        u'http://thetvdb.com/banners/actors/43640.jpg'

        Any key starting with an underscore has been processed (not the raw
        data from the XML)
        """
        logger.debug('Getting actors for %s', sid)

        actors = self.config['session'].series_api.series_id_actors_get(sid)

        if not actors or not actors.data:
            logger.debug('Actors result returned zero')
            return

        cur_actors = Actors()
        for cur_actor in actors.data if isinstance(actors.data, list) else [actors.data]:
            new_actor = Actor()
            new_actor['id'] = cur_actor.id
            new_actor['image'] = self.config['artwork_prefix'] % cur_actor.image
            new_actor['name'] = cur_actor.name
            new_actor['role'] = cur_actor.role
            new_actor['sortorder'] = 0
            cur_actors.append(new_actor)
        self._set_show_data(sid, '_actors', cur_actors)

    def _get_show_data(self, sid, language):
        """Parse TheTVDB json response.

        Takes a series ID, gets the epInfo URL and parses the TheTVDB json response
        into the shows dict in layout:
        shows[series_id][season_number][episode_number]
        """
        if self.config['language'] is None:
            logger.debug('Config language is none, using show language')
            if language is None:
                raise IndexerError("config['language'] was None, this should not happen")
            get_show_in_language = language
        else:
            logger.debug(
                'Configured language %s override show language of %s' % (
                    self.config['language'],
                    language
                )
            )
            get_show_in_language = self.config['language']

        # Parse show information
        logger.debug('Getting all series data for %s' % sid)

        # Parse show information
        series_info = self._get_show_by_id(sid, request_language=get_show_in_language)

        if not series_info:
            logger.debug('Series result returned zero')
            raise IndexerError('Series result returned zero')

        # get series data / add the base_url to the image urls
        for k, v in series_info['series'].items():
            if v is not None:
                if k in ['banner', 'fanart', 'poster']:
                    v = self.config['artwork_prefix'] % v
            self._set_show_data(sid, k, v)

        # Create the externals structure
        self._set_show_data(sid, 'externals', {'imdb_id': str(getattr(self[sid], 'imdb_id', ''))})

        # get episode data
        if self.config['episodes_enabled']:
            self._get_episodes(sid, specials=False, aired_season=None)

        # Parse banners
        if self.config['banners_enabled']:
            self._parse_images(sid)

        # Parse actors
        if self.config['actors_enabled']:
            self._parse_actors(sid)

        return True

    # Public methods, usable separate from the default api's interface api['show_id']
    @plex_fallback
    def get_last_updated_series(self, from_time, weeks=1, filter_show_list=None):
        """Retrieve a list with updated shows.

        :param from_time: epoch timestamp, with the start date/time
        :param weeks: number of weeks to get updates for.
        :param filter_show_list: Optional list of show objects, to use for filtering the returned list.
        :returns: A list of show_id's.
        """
        total_updates = []
        updates = True

        count = 0
        try:
            while updates and count < weeks:
                updates = self.config['session'].updates_api.updated_query_get(from_time).data
                if updates is not None:
                    last_update_ts = max(x.last_updated for x in updates)
                    from_time = last_update_ts
                    total_updates += [int(_.id) for _ in updates]
                count += 1
        except ApiException as e:
            if e.status == 401:
                raise IndexerAuthFailed(
                    'Authentication failed, possible bad api key. reason: {reason} ({status})'
                    .format(reason=e.reason, status=e.status)
                )
            raise IndexerUnavailable('Error connecting to Tvdb api. Caused by: {0}'.format(e.message))
        except RequestException as e:
            raise IndexerUnavailable('Error connecting to Tvdb api. Caused by: {0}'.format(e.message))

        if total_updates and filter_show_list:
            new_list = []
            for show in filter_show_list:
                if show.indexerid in total_updates:
                    new_list.append(show.indexerid)
            total_updates = new_list

        return total_updates

    # Public methods, usable separate from the default api's interface api['show_id']
    def get_last_updated_seasons(self, show_list, from_time, weeks=1):
        """Return updated seasons for shows passed, using the from_time.

        :param show_list[int]: The list of shows, where seasons updates are retrieved for.
        :param from_time[int]: epoch timestamp, with the start date/time
        :param weeks: number of weeks to get updates for.
        """
        show_season_updates = {}

        for show_id in show_list:
            total_updates = []
            # Get the shows episodes using the GET /series/{id}/episodes route, and use the lastUpdated attribute
            # to check if the episodes season should be updated.
            logger.debug('Getting episodes for {show_id}', show_id=show_id)
            episodes = self._download_episodes(show_id)

            for episode in episodes['episode']:
                if episode.get('seasonnumber') is None or episode.get('episodenumber') is None:
                    logger.warning('This episode has incomplete information. The season or episode number '
                                   '(season: %s, episode: %s) is missing. '
                                   'to get rid of this warning, you will have to contact tvdb through their forums '
                                   'and have them fix the specific episode.',
                                   episode.get('seasonnumber'), episode.get('episodenumber'))
                    continue

                if int(episode['lastupdated']) > from_time:
                    total_updates.append(int(episode['seasonnumber']))

            show_season_updates[show_id] = list(set(total_updates))

        return show_season_updates
