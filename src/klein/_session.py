
from typing import Any, Callable, Optional as _Optional, TYPE_CHECKING, Union

import attr

from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.python.reflect import qual
from twisted.web.http import UNAUTHORIZED
from twisted.web.resource import Resource

from zope.interface import implementer
from zope.interface.interfaces import IInterface

from .interfaces import (
    EarlyExit, IDependencyInjector, IRequestLifecycle, IRequiredParameter,
    ISession, ISessionProcurer, ISessionStore, NoSuchSession, SessionMechanism,
    TooLateForCookies
)

if TYPE_CHECKING:
    from twisted.web.iweb import IRequest
    from twisted.python.components import Componentized
    from mypy_extensions import KwArg, VarArg, Arg
    from typing import TypeVar, Awaitable, Dict
    T = TypeVar('T')
    (IRequest, Arg, KwArg, VarArg, Callable, Dict, IInterface, Awaitable,
     Componentized, IRequestLifecycle)
else:
    Arg = KwArg = lambda t, *x: t




@implementer(ISessionProcurer)
@attr.s
class SessionProcurer(object):
    """
    A L{SessionProcurer} procures a session from a request and a store.

    @ivar _store: The session store to procure a session from.
    @type _store: L{klein.interfaces.ISessionStore}

    @ivar _maxAge: The maximum age (in seconds) of the session cookie.
    @type _maxAge: L{int}

    @ivar _secureCookie: The name of the cookie to use for sessions protected
        with TLS (i.e. HTTPS).
    @type _secureCookie: L{bytes}

    @ivar _insecureCookie: The name of the cookie to use for sessions I{not}
        protected with TLS (i.e. HTTP).
    @type _insecureCookie: L{bytes}

    @ivar _cookieDomain: If set, the domain name to restrict the session cookie
        to.
    @type _cookieDomain: L{None} or L{bytes}

    @ivar _cookiePath: If set, the URL path to restrict the session cookie to.
    @type _cookiePath: L{bytes}

    @ivar _secureTokenHeader: The name of the HTTPS header to try to extract a
        session token from; API clients should use this header, rather than a
        cookie.
    @type _secureTokenHeader: L{bytes}

    @ivar _insecureTokenHeader: The name of the HTTP header to try to extract a
        session token from; API clients should use this header, rather than a
        cookie.
    @type _insecureTokenHeader: L{bytes}

    @ivar _setCookieOnGET: Automatically request that the session store create
        a session if one is not already associated with the request and the
        request is a GET.
    @type _setCookieOnGET: L{bool}
    """

    _store = attr.ib(type=ISessionStore)

    _maxAge = attr.ib(type=int, default=3600)
    _secureCookie = attr.ib(type=bytes, default=b"Klein-Secure-Session")
    _insecureCookie = attr.ib(type=bytes, default=b"Klein-INSECURE-Session")
    _cookieDomain = attr.ib(type=_Optional[bytes], default=None)
    _cookiePath = attr.ib(type=bytes, default=b"/")

    _secureTokenHeader = attr.ib(type=bytes, default=b"X-Auth-Token")
    _insecureTokenHeader = attr.ib(type=bytes,
                                   default=b"X-INSECURE-Auth-Token")
    _setCookieOnGET = attr.ib(type=bool, default=True)

    @inlineCallbacks
    def procureSession(self, request, forceInsecure=False):
        # type: (IRequest, bool) -> Any
        alreadyProcured = request.getComponent(ISession)
        if alreadyProcured is not None:
            returnValue(alreadyProcured)

        if request.isSecure():
            if forceInsecure:
                tokenHeader = self._insecureTokenHeader
                cookieName = self._insecureCookie
                sentSecurely = False
            else:
                tokenHeader = self._secureTokenHeader
                cookieName = self._secureCookie
                sentSecurely = True
        else:
            # Have we inadvertently disclosed a secure token over an insecure
            # transport, for example, due to a buggy client?
            allPossibleSentTokens = (
                sum([request.requestHeaders.getRawHeaders(header, [])
                     for header in [self._secureTokenHeader,
                                    self._insecureTokenHeader]], []) +
                [it for it in [request.getCookie(cookie)
                               for cookie in [self._secureCookie,
                                              self._insecureCookie]] if it]
            )
            # Does it seem like this check is expensive? It sure is! Don't want
            # to do it? Turn on your dang HTTPS!
            yield self._store.sentInsecurely(allPossibleSentTokens)
            tokenHeader = self._insecureTokenHeader
            cookieName = self._insecureCookie
            sentSecurely = False
            # Fun future feature: honeypot that does this over HTTPS, but sets
            # isSecure() to return false because it serves up a cert for the
            # wrong hostname or an invalid cert, to keep API clients honest
            # about chain validation.
        sessionID = request.getHeader(tokenHeader)
        if sessionID is not None:
            mechanism = SessionMechanism.Header
        else:
            mechanism = SessionMechanism.Cookie
            sessionID = request.getCookie(cookieName)
        if sessionID is not None:
            sessionID = sessionID.decode('ascii')
            try:
                session = yield self._store.loadSession(
                    sessionID, sentSecurely, mechanism
                )
            except NoSuchSession:
                if mechanism == SessionMechanism.Header:
                    raise
                sessionID = None
        if sessionID is None:
            if request.method != b'GET' or not self._setCookieOnGET:
                # If we don't have a session ID at all, and we're not allowed
                # to set a cookie on the client, don't waste session-store
                # resources by allocating one.
                returnValue(None)
            if request.startedWriting:
                # At this point, if the mechanism is Header, we either have
                # a valid session or we bailed after NoSuchSession above.
                raise TooLateForCookies(
                    "You tried initializing a cookie-based session too"
                    " late in the request pipeline; the headers"
                    " were already sent."
                )
            session = yield self._store.newSession(sentSecurely, mechanism)
        if (
                sessionID != session.identifier and
                request.method == b'GET' and
                self._setCookieOnGET
        ):
            # sessionID is the input session ID from the request;
            # session.identifier is the created or loaded session from the
            # session store.  This cookie is set when setCookiesOnGET is
            # allowed, either when the session store has informed of us of a
            # changed session identifier or when a new session has been created
            # (sessionID is None)
            if request.startedWriting:
                raise TooLateForCookies(
                    "You tried changing a session ID to a new session ID too"
                    " late in the request pipeline; the headers were already"
                    " sent."
                )
            request.addCookie(
                cookieName, session.identifier, max_age=self._maxAge,
                domain=self._cookieDomain, path=self._cookiePath,
                secure=sentSecurely, httpOnly=True,
            )
        if sentSecurely or not request.isSecure():
            # Do not cache the insecure session on the secure request, thanks.
            request.setComponent(ISession, session)
        returnValue(session)


_procureProcurerType = Union[
    Callable[[Any], ISessionProcurer],
    Callable[[], ISessionProcurer]
]

_kleinRenderable = Any
_routeCallable = Any
_kleinCallable = Callable[..., _kleinRenderable]
_kleinDecorator = Callable[[_kleinCallable], _kleinCallable]
_requirerResult = Callable[[Arg(_routeCallable, 'route'), KwArg(Any)],
                           Callable[[_kleinCallable], _kleinCallable]]


class AuthorizationDenied(Resource, object):
    def __init__(self, interface):
        # type: (IInterface) -> None
        self._interface = interface
        super(AuthorizationDenied, self).__init__()

    def render(self, request):
        # type: (IRequest) -> bytes
        request.setResponseCode(UNAUTHORIZED)
        return "{} DENIED".format(qual(self._interface)).encode('utf-8')


@implementer(IDependencyInjector, IRequiredParameter)
@attr.s
class Authorization(object):
    """
    Authorize.
    """
    _interface = attr.ib(type=IInterface)
    _required = attr.ib(type=bool, default=True)

    @classmethod
    def optional(cls, interface):
        # type: (IInterface) -> Authorization
        """
        Make a requirement passed to L{Authorizer.require}.
        """
        return cls(interface, required=False)


    def registerInjector(self, injectionComponents, parameterName, lifecycle):
        # type: (Componentized, str, IRequestLifecycle) -> IDependencyInjector
        """
        Register this authorization to inject a parameter.
        """
        return self


    @inlineCallbacks
    def injectValue(self, request):
        # type: (IRequest) -> Any
        """
        Inject a value by asking the request's session.
        """
        # TODO: this could be optimized to do fewer calls to 'authorize' by
        # collecting all the interfaces that are necessary and then using
        # addBeforeHook; the interface would not need to change.
        provider = ((yield ISession(request).authorize([self._interface]))
                    .get(self._interface))
        if self._required and provider is None:
            raise EarlyExit(AuthorizationDenied(self._interface))
        # TODO: CSRF protection should probably go here
        returnValue(provider)


    def finalize(self):
        # type: () -> None
        """
        Nothing to finalize when registering.
        """
