#!/usr/bin/env python

"""
conference.py -- Udacity conference server-side Python App Engine API;
    uses Google Cloud Endpoints

$Id: conference.py,v 1.25 2014/05/24 23:42:19 wesc Exp wesc $

created by wesc on 2014 apr 21

"""

__author__ = 'wesc+api@google.com (Wesley Chun)'


from datetime import datetime

import endpoints
from protorpc import messages
from protorpc import message_types
from protorpc import remote

from google.appengine.ext import ndb
from google.appengine.api import memcache
from google.appengine.api import taskqueue

from models import Profile
from models import ProfileMiniForm
from models import ProfileForm
from models import TeeShirtSize

from models import Conference
from models import ConferenceForm
from models import Session
from models import SessionForm
from models import SessionForms
from models import SpeakerForm
from models import Speaker

from models import ConferenceForms
from models import ConferenceQueryForm
from models import ConferenceQueryForms

from models import BooleanMessage
from models import ConflictException

from models import StringMessage

from utils import getUserId

from settings import WEB_CLIENT_ID
from settings import ANDROID_CLIENT_ID
from settings import IOS_CLIENT_ID
from settings import ANDROID_AUDIENCE

EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID

MEMCACHE_ANNOUNCEMENTS_KEY = "RECENT_ANNOUNCEMENTS"
ANNOUNCEMENT_TPL = ('Last chance to attend! The following conferences '
                    'are nearly sold out: %s')

#- - - - - - - - - - - - - - - - - - - - - - - - - 

DEFAULTS = {
    "city": "Default City",
    "maxAttendees": 0,
    "seatsAvailable": 0,
    "topics": [ "Default", "Topic" ],
}

OPERATORS = {
            'EQ':   '=',
            'GT':   '>',
            'GTEQ': '>=',
            'LT':   '<',
            'LTEQ': '<=',
            'NE':   '!='
            }

FIELDS =    {
            'CITY': 'city',
            'TOPIC': 'topics',
            'MONTH': 'month',
            'MAX_ATTENDEES': 'maxAttendees',
            }

CONF_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)

CONF_POST_REQUEST = endpoints.ResourceContainer(
    ConferenceForm,
    websafeConferenceKey=messages.StringField(1),
)

SESSION_GET_REQUEST = endpoints.ResourceContainer(
    SessionForm,
    websafeConferenceKey=messages.StringField(1),
)

SESSION_BY_TYPE = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
    type=messages.StringField(2),
)

SESSION_POST_REQUEST = endpoints.ResourceContainer(
    SessionForm,
    websafeConferenceKey=messages.StringField(1, required=True),
)

SESSION_BY_SPEAKER = endpoints.ResourceContainer(
    message_types.VoidMessage,
    speakerKey=messages.StringField(1),
)

SESSION_WISHLIST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeSessionKey=messages.StringField(1),
)


# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

@endpoints.api(name='conference', version='v1', audiences=[ANDROID_AUDIENCE],
    allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID, ANDROID_CLIENT_ID, IOS_CLIENT_ID],
    scopes=[EMAIL_SCOPE])
class ConferenceApi(remote.Service):
    """Conference API v0.1"""

# - - - Conference objects - - - - - - - - - - - - - - - - -

    def _copyConferenceToForm(self, conf, displayName):
        """Copy relevant fields from Conference to ConferenceForm."""
        cf = ConferenceForm()
        for field in cf.all_fields():
            if hasattr(conf, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(cf, field.name, str(getattr(conf, field.name)))
                else:
                    setattr(cf, field.name, getattr(conf, field.name))
            elif field.name == "websafeKey":
                setattr(cf, field.name, conf.key.urlsafe())
        if displayName:
            setattr(cf, 'organizerDisplayName', displayName)
        cf.check_initialized()
        return cf


    def _createConferenceObject(self, request):
        """Create or update Conference object, returning ConferenceForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException("Conference 'name' field required")

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['organizerDisplayName']

        # add default values for those missing (both data model & outbound Message)
        for df in DEFAULTS:
            if data[df] in (None, []):
                data[df] = DEFAULTS[df]
                setattr(request, df, DEFAULTS[df])

        # convert dates from strings to Date objects; set month based on start_date
        if data['startDate']:
            data['startDate'] = datetime.strptime(data['startDate'][:10], "%Y-%m-%d").date()
            data['month'] = data['startDate'].month
        else:
            data['month'] = 0
        if data['endDate']:
            data['endDate'] = datetime.strptime(data['endDate'][:10], "%Y-%m-%d").date()

        # set seatsAvailable to be same as maxAttendees on creation
        # both for data model & outbound Message
        if data["maxAttendees"] > 0:
            data["seatsAvailable"] = data["maxAttendees"]
            setattr(request, "seatsAvailable", data["maxAttendees"])

        # make Profile Key from user ID
        p_key = ndb.Key(Profile, user_id)
        # allocate new Conference ID with Profile key as parent
        c_id = Conference.allocate_ids(size=1, parent=p_key)[0]
        # make Conference key from ID
        c_key = ndb.Key(Conference, c_id, parent=p_key)
        data['key'] = c_key
        data['organizerUserId'] = request.organizerUserId = user_id

        # create Conference & return (modified) ConferenceForm
        Conference(**data).put()
        #adding confirmation email sending task to queue
        taskqueue.add(params={'email': user.email(),
            'conferenceInfo': repr(request)},
            url='/tasks/send_confirmation_email'
        )

        return request

    @ndb.transactional()
    def _updateConferenceObject(self, request):
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the owner can update the conference.')
        for field in request.all_fields():
            data = getattr(request, field.name)
            if data not in (None, []):
                if field.name in ('startDate', 'endDate'):
                    data = datetime.strptime(data, "%Y-%m-%d").date()
                    if field.name == 'startDate':
                        conf.month = data.month
                setattr(conf, field.name, data)
        conf.put()
        prof = ndb.Key(Profile, user_id).get()
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))

# - - - - - - - - Session Objects - - - - - - - - - - - - -

    def _copySessionToForm(self, session, name=None):
        """Copy relevant fields from Session to SessionForm."""
        session_form = SessionForm()
        for field in session_form.all_fields():
            if hasattr(session, field.name):
                # convert typeOfSession to enum SessionTypes; just copy others
                if field.name == 'typeOfSession':
                    setattr(session_form, field.name, getattr(SessionTypes, str(getattr(session,field.name))))
                else:
                    setattr(session_form, field.name, getattr(session,field.name))
            elif field.name == "websafeKey":
                setattr(session_form, field.name, session.key.urlsafe())
            elif field.name == "speakerDisplayName":
                setattr(session_form, field.name, name)

            # convert startDateTime from session model to date and startTime for session Form
            startDateTime = getattr(session, 'startDateTime')
            if startDateTime:
                if field.name == 'date':
                    setattr(session_form, field.name, str(startDateTime.date()))
                if hasattr(session, 'startDateTime') and field.name == 'startTime':
                    setattr(session_form, field.name, str(startDateTime.time().strftime('%H:%M')))
        session_form.check_initialized()
        return session_form

    def _createSessionObject(self, request):
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException("Session 'name' field required")

        # copy SessionForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeConferenceKey']
        del data['websafeKey']
    
        # add default values for those missing (both data model & outbound Message)
        for defaultValue in SESSION_DEFAULTS:
            if data[defaultValue] in (None, []):
                data[default] = SESSION_DEFAULTS[defaultValue]
                setattr(request, defaulValue, SESSION_DEFAULTS[defaulValue])

        if data['typeOfSession']==None:
            del data['typeOfSession']
        else:
            data['typeOfSession'] = str(data['typeOfSession'])

        # set start time and date to be next available when not explicit
        if data['startTime'] and data['date']:
            data['startDateTime'] = datetime.strptime(data['date'][:10] + ' ' + data['startTime'][:5], "%Y-%m-%d %H:%M")
        del data['startTime']
        del data['date']

        # get the conference for session
        c_key = ndb.Key(urlsafe=request.websafeConferenceKey).get()

        # check that conf.key is a Conference key and it exists
        if not c_key:
            raise endpoints.NotFoundException(
                'Hold up! No conference with key %s' % request.websafeConferenceKey)

        # check that user is owner
        if user_id != c_key.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the owner can update the conference.')

        # generate Session key as child of Conference
        s_id = Session.allocate_ids(size=1, parent=c_key.key)[0]
        s_key = ndb.Key(Session, s_id, parent=c_key.key)
        data['key'] = s_key

        # get the speakerDisplayName from Speaker entity if a speakerKey was provided
        if data['speakerKey']:
            speaker = ndb.Key(urlsafe=request.s_key).get()

            # check that speaker.key is a speaker key and it exists
            if not s_key:
                raise endpoints.NotFoundException(
                'Hold up! No speaker with key %s' % request.websafeConferenceKey)

            data['speakerDisplayName'] = speaker.displayName

# - - - - - - - - - Query and Filter Objects - - - - - - - - - -

    def _getQuery(self, request):
        """Return formatted query from the submitted filters."""
        q = Conference.query()
        inequality_filter, filters = self._formatFilters(request.filters)

        # If exists, sort on inequality filter first
        if not inequality_filter:
            q = q.order(Conference.name)
        else:
            q = q.order(ndb.GenericProperty(inequality_filter))
            q = q.order(Conference.name)

        for filtr in filters:
            if filtr["field"] in ["month", "maxAttendees"]:
                filtr["value"] = int(filtr["value"])
            formatted_query = ndb.query.FilterNode(filtr["field"], filtr["operator"], filtr["value"])
            q = q.filter(formatted_query)
        return q


    def _formatFilters(self, filters):
        """Parse, check validity and format user supplied filters."""
        formatted_filters = []
        inequality_field = None

        for f in filters:
            filtr = {field.name: getattr(f, field.name) for field in f.all_fields()}

            try:
                filtr["field"] = FIELDS[filtr["field"]]
                filtr["operator"] = OPERATORS[filtr["operator"]]
            except KeyError:
                raise endpoints.BadRequestException("Filter contains invalid field or operator.")

            # Every operation except "=" is an inequality
            if filtr["operator"] != "=":
                # check if inequality operation has been used in previous filters
                # disallow the filter if inequality was performed on a different field before
                # track the field on which the inequality operation is performed
                if inequality_field and inequality_field != filtr["field"]:
                    raise endpoints.BadRequestException("Inequality filter is allowed on only one field.")
                else:
                    inequality_field = filtr["field"]

            formatted_filters.append(filtr)
        return (inequality_field, formatted_filters)

# - - - - Conference Endpoints - - - - - - - - - - - - - - -

    #create conferences
    @endpoints.method(ConferenceForm, ConferenceForm, 
            path='conference/create',
            http_method='POST', name='createConference')
    def createConference(self, request):
        """make a new conference"""
        return self._createConferenceObject(request)

    #get conferences that have been created
    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='getConferencesCreated',
            http_method='POST', name='getConferencesCreated')
    def getConferencesCreated(self, request):
        """Return user created conferences."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        #query conferences
        conferences = Conference.query(ancestor=ndb.Key(Profile, getUserId(user)))
        #profile key
        prof = ndb.Key(Profile, getUserId(user)).get()
        #return conf form obj
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, getattr(prof, 'displayName')) for conf in conferences]
        )

    #query conferences
    @endpoints.method(ConferenceQueryForms, ConferenceForms,
            path='queryConferences',
            http_method='POST',
            name='queryConferences')
    def queryConferences(self, request):
        """Query for conferences."""
        conferences = self._getQuery(request)

        # need to fetch organiser displayName from profiles
        # get all keys and use get_multi for speed
        organisers = [(ndb.Key(Profile, conf.organizerUserId)) for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return individual ConferenceForm object per Conference
        return ConferenceForms(
                items=[self._copyConferenceToForm(conf, names[conf.organizerUserId]) for conf in \
                conferences]
        )

    #getPartialConferences
    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='conferences/partial_conferences',
            http_method='GET', name='getPartialConferences')
    def getPartialConferences(self, request):
        """Get list of all conferences that need additional information"""
        conferences = Conference.query(ndb.OR(
                Conference.description==None,
                Conference.startDate==None,
                Conference.endDate==None))
        items = [self._copyConferenceToForm(conf, getattr(conf.key.parent().get(), 'displayName')) for conference in conferences]

        return ConferenceForms(items=items)

# - - - - - - - Session Endpoints - - - - - - - - - - - - - - - -

    #createSession(SessionForm, websafeConferenceKey) -- open only to the organizer of the conference
    @endpoints.method(SESSION_POST_REQUEST, SessionForm,
            path='conference/sessions',
            http_method='POST', name='createSession')
    def createSession(self, request):
        """Create a new session for a conference. Open only to the organizer of the conference"""
        return self._createSessionObject(request)

    #getConferenceSessions(websafeConferenceKey) -- Given a conference, return all sessions
    @endpoints.method(CONF_GET_REQUEST, SessionForms,
        path='conference/get_sessions',
        http_method='GET', name='getConferenceSessions')
    def getConferenceSessions(self, request):
        """Retrieve sessions in a conference"""
        #get key
        c_key = ndb.Key(urlsafe=request.websafeConferenceKey).get()

        #check that it's legit
        if not c_key:
            raise endpoints.NotFoundException(
                'Hold up! No conference with key %s' % request.websafeConferenceKey)

        #get specific conference's sessions
        sessionsList = session.Query(ancestor=ndb.key(Conference, c_key.key.id()))

        #show sessions
        return SessionForms(
            items=[self._copySessionToForm(session) for session in sessions]
        )
    
    #getConferenceSessionsByType(websafeConferenceKey, typeOfSession) Given a conference, return all sessions of a specified type (eg lecture, keynote, workshop)
    @endpoints.method(SESSION_GET_REQUEST, SessionForms,
        path='conference/sessions/by_type',
        http_method='GET', name='getConferenceSessionsByType')
    def getConferenceSessionsByType(self, request):
        """return all sessions of the same type at a conference"""

        #get key
        c_key = ndb.Key(urlsafe=request.websafeConferenceKey).get()

        #check that it's legit
        if not c_key:
            raise endpoints.NotFoundException(
                'Hold up! No conference with key %s' % request.websafeConferenceKey)

        #get specific conference's sessions by type
        sessionsList = session.Query(Session.typeOfSession==typeOfSession, ancestor=ndb.key(Conference, c_key.key.id()))

        #show sessions
        return SessionForms(
            items=[self._copySessionToForm(session) for session in sessions]
        )

    #getSessionsBySpeaker(speaker) -- Given a speaker, return all sessions given by this particular speaker, across all conferences
    @endpoints.method(SESSION_BY_SPEAKER, SessionForms,
        path='sessions/by_speaker',
        http_method='GET', name='getSessionsBySpeaker')
    def getSessionsBySpeaker(self, request):
        """Return all sessions by specific speaker"""
        if request.speakerKey:
            sessions.sessions.filter(Session.speakerKey == request.speakerKey)
        return SessionForms(
            items=[self._copySessionToForm(session) for session in sessions]
        )

    #getPartialSessions
    @endpoints.method(CONF_GET_REQUEST, SessionForms,
        path='conference/partial_sessions',
        http_method='GET', name='getPartialSessions')
    def getPartialSessions(self, request):
        """Return sessions with missing info"""
        c_key=ndb.Key(urlsafe=request.websafeConferenceKey).get()

        if not c_key:
            raise endpoints.NotFoundException(
                'Hold up! No conference with key %s' % request.websafeConferenceKey)

        partial_sessions=Session.query(ndb.OR(Session.highlights=='',Session.speaker=='',
            Session.duration==None,Session.typeOfSession==None,Session.sessionDate==None,
            Session.startTime==None), ancestor=c_key)

        return SessionForms(items=[self._copySessionToForm(session) for session in sessions])

    #addSessionToWishlist(SessionKey) -- adds the session to the user's list of sessions they are interested in attending
    @endpoints.method(SESSION_WISHLIST, SessionForm,
            http_method='POST', name='addSessionToWishlist')
    def addSessionToWishlist(self, request):
        """Saves a session to wishlist"""
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        # fetch and check session
        session = ndb.Key(urlsafe=request.websafeSessionKey).get()

        # check session exists
        if not session:
            raise endpoints.NotFoundException(
                'Hold up! No session with key: %s' % request.websafeSessionKey)

        prof = self._getProfileFromUser()

        # check if session already in wishlist
        if session.key in prof.sessionsToAttend:
            raise endpoints.BadRequestException(
                'Session already saved: %s' % request.websafeSessionKey)

        # add to wishlist
        prof.sessionsToAttend.append(session.key)
        prof.put()

        return self._copySessionToForm(session)

    #getSessionsInWishlist() -- query for all the sessions in a conference that the user is interested in
    @endpoints.method(message_types.VoidMessage, SessionForms,
            http_method='POST', name='getSessionsInWishlist')
    def getSessionsInWishlist(self, request):
        """Returns a user's wishlist of sessions"""
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        # get profile and wishlist
        prof = self._getProfileFromUser()
        session_keys = prof.sessionsToAttend
        sessions = [session_key.get() for session_key in session_keys]

        return SessionForms(
            items=[self._copySessionToForm(session) for session in sessions]
        )

# - - - - - - - - Speaker Endpoints - - - - - - - - - - - - - - - -

    #Define the following Endpoints method: getFeaturedSpeaker()
    @endpoints.method(message_types.VoidMessage, SpeakerForm,
            http_method='GET', name='getFeaturedSpeaker')
    def getFeaturedSpeaker(self, request):
        """Returns the sessions of the featured speaker"""
        data = memcache.get('featured_speaker')
        from pprint import pprint
        pprint(data)
        sessions = []
        sessionNames = []
        speaker = None

        if data and data.has_key('speaker') and data.has_key('sessionNames'):
            speaker = data['speaker']
            sessionNames = data['sessionNames']
        else:
            upcoming_session = Session.query(Session.date >= datetime.now())\
                                    .order(Session.date, Session.startTime).get()
            if upcoming_session:
                speaker = upcoming_session.speaker
                sessions = Session.query(Session.speaker == speaker)
                sessionNames = [session.name for session in sessions]

        # create speaker form
        sf = SpeakerForm()
        for field in sf.all_fields():
            if field.name == 'sessionNames':
                setattr(sf, field.name, sessionNames)
            elif field.name == 'speaker':
                setattr(sf, field.name, speaker)
        sf.check_initialized()
        return sf

# - - - Profile objects - - - - - - - - - - - - - - - - - - -

    def _copyProfileToForm(self, prof):
        """Copy relevant fields from Profile to ProfileForm."""
        # copy relevant fields from Profile to ProfileForm
        pf = ProfileForm()
        for field in pf.all_fields():
            if hasattr(prof, field.name):
                # convert t-shirt string to Enum; just copy others
                if field.name == 'teeShirtSize':
                    setattr(pf, field.name, getattr(TeeShirtSize, getattr(prof, field.name)))
                else:
                    setattr(pf, field.name, getattr(prof, field.name))
        pf.check_initialized()
        return pf


    def _getProfileFromUser(self):
        """Return user Profile from datastore, creating new one if non-existent."""
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        #get profile info
        user_id=getUserId(user)
        p_key = ndb.Key(Profile, user_id)
        profile = p_key.get()

        #create new if no profile found
        if not profile:
            profile = Profile(
                key = p_key,
                displayName = user.nickname(), 
                mainEmail= user.email(),
                teeShirtSize = str(TeeShirtSize.NOT_SPECIFIED),
            )
            profile.put()

        return profile      # return Profile


    def _doProfile(self, save_request=None):
        """Get user Profile and return to user, possibly updating it first."""
        # get user Profile
        prof = self._getProfileFromUser()

        # if saveProfile(), process user-modifyable fields
        if save_request:
            for field in ('displayName', 'teeShirtSize'):
                if hasattr(save_request, field):
                    val = getattr(save_request, field)
                    if val:
                        setattr(prof, field, str(val))
            prof.put()

        # return ProfileForm
        return self._copyProfileToForm(prof)


    @endpoints.method(message_types.VoidMessage, ProfileForm,
            path='profile', http_method='GET', name='getProfile')
    def getProfile(self, request):
        """Return user profile."""
        return self._doProfile()

    @endpoints.method(ProfileMiniForm, ProfileForm,
            path='profile', http_method='POST', name='saveProfile')
    def saveProfile(self, request):
        """Update & return user profile."""
        return self._doProfile(request)

# - - - Announcements - - - - - - - - - - - - - - - - - - - -

    @staticmethod
    def _cacheAnnouncement():
        """Create Announcement & assign to memcache; used by
        memcache cron job & putAnnouncement().
        """
        confs = Conference.query(ndb.AND(
            Conference.seatsAvailable <= 5,
            Conference.seatsAvailable > 0)
        ).fetch(projection=[Conference.name])

        if confs:
            # If there are almost sold out conferences,
            # format announcement and set it in memcache
            announcement = ANNOUNCEMENT_TPL % (
                ', '.join(conf.name for conf in confs))
            memcache.set(MEMCACHE_ANNOUNCEMENTS_KEY, announcement)
        else:
            # If there are no sold out conferences,
            # delete the memcache announcements entry
            announcement = ""
            memcache.delete(MEMCACHE_ANNOUNCEMENTS_KEY)

        return announcement


    @endpoints.method(message_types.VoidMessage, StringMessage,
            path='conference/announcement/get',
            http_method='GET', name='getAnnouncement')
    def getAnnouncement(self, request):
        """Return Announcement from memcache."""
        return StringMessage(data=memcache.get(MEMCACHE_ANNOUNCEMENTS_KEY) or "")

# - - - Registration - - - - - - - - - - - - - - - - - - - -

    @ndb.transactional(xg=True)
    def _conferenceRegistration(self, request, reg=True):
        """Register or unregister user for selected conference."""
        retval = None
        prof = self._getProfileFromUser() # get user Profile

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % wsck)

        # register
        if reg:
            # check if user already registered otherwise add
            if wsck in prof.conferenceKeysToAttend:
                raise ConflictException(
                    "You have already registered for this conference")

            # check if seats avail
            if conf.seatsAvailable <= 0:
                raise ConflictException(
                    "There are no seats available.")

            # register user, take away one seat
            prof.conferenceKeysToAttend.append(wsck)
            conf.seatsAvailable -= 1
            retval = True

        # unregister
        else:
            # check if user already registered
            if wsck in prof.conferenceKeysToAttend:

                # unregister user, add back one seat
                prof.conferenceKeysToAttend.remove(wsck)
                conf.seatsAvailable += 1
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        conf.put()
        return BooleanMessage(data=retval)

    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='conferences/attending',
            http_method='GET', name='getConferencesToAttend')
    def getConferencesToAttend(self, request):
        """Get list of conferences that user has registered for."""
        prof = self._getProfileFromUser()
        conf_keys = [ndb.Key(urlsafe=wsck) for wsck in prof.conferenceKeysToAttend]
        conferences = ndb.get_multi(conf_keys)
        organisers = [ndb.Key(Profile, conf.organizerUserId) for conf in conferences]
        profiles = ndb.get_multi(organisers)
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName
        # return set of ConferenceForm objects per Conference
        return ConferenceForms(items=[self._copyConferenceToForm(conf, names[conf.organizerUserId])\
         for conf in conferences]
        )


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/register',
            http_method='POST', name='registerForConference')
    def registerForConference(self, request):
        """Register user for selected conference."""
        return self._conferenceRegistration(request)

# registers API
    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/unregister',
            http_method='DELETE', name='unregisterFromConference')
    def unregisterFromConference(self, request):
        """Unregister user for selected conference."""
        return self._conferenceRegistration(request, reg=False)
    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='filterPlayground',
            http_method='GET', name='filterPlayground')
    def filterPlayground(self, request):
        """Filter Playground"""
        q = Conference.query()
        q = q.filter(Conference.city=="London")
        q = q.filter(Conference.topics=="Medical Innovations")
        q = q.filter(Conference.month==6)
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") for conf in q]
        )
api = endpoints.api_server([ConferenceApi]) 
