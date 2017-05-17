#!/usr/bin/env python
# -*- coding: utf-8 -*-
# pylint: disable=fixme,missing-docstring

from __future__ import print_function, division
import collections
import itertools
import os
import sys
import time

import flask
import flask_pymongo
import gunicorn.app.base
import raven.contrib.flask
from slacker import Slacker, Error

# noinspection PyUnresolvedReferences
import bootstrap  # noqa # pylint: disable=unused-import
import slack_archive
import store


SLACK_CLIENT_ID = os.environ['SLACK_CLIENT_ID']
SLACK_CLIENT_SECRET = os.environ['SLACK_CLIENT_SECRET']
SLACK_TEAM_ID = os.environ['SLACK_TEAM_ID']
SLACK_TEAM_TOKEN = os.environ['SLACK_TEAM_TOKEN']
MONGO_URI = os.environ['MONGO_URI']
CRYPTO_KEY = os.environ['CRYPTO_KEY']
SENTRY_URI = os.environ['SENTRY_URI']

BASIC_LINK = ('https://slack.com/oauth/authorize?team=' + SLACK_TEAM_ID +
              '&client_id=' + SLACK_CLIENT_ID + '&scope=team:read,' +
              'users:read,channels:read,channels:history,pins:read,emoji:read')
LOGIN_LINK = ('https://slack.com/oauth/authorize?team=' + SLACK_TEAM_ID +
              '&client_id=' + SLACK_CLIENT_ID + '&scope=identify')
TOKEN_LINK = ('https://slack.com/oauth/authorize?team=' + SLACK_TEAM_ID +
              '&client_id=' + SLACK_CLIENT_ID + '&scope=identify,files:read,' +
              'channels:read,channels:history,groups:history,groups:read,'
              'im:history,im:read,mpim:read,mpim:history,stars:read')


class FlaskExt(flask.Flask):
    _HOOK_ROUTE_PROP = 'flask_ext_route'
    RouteArgs = collections.namedtuple('RouteArgs', ['args', 'kwargs'])

    @staticmethod
    def route(*args, **kwargs):
        def wrap(method):
            route_rules = getattr(method, FlaskExt._HOOK_ROUTE_PROP, [])
            route_rules.append(FlaskExt.RouteArgs(args, kwargs))
            setattr(method, FlaskExt._HOOK_ROUTE_PROP, route_rules)
            return method
        return wrap

    def _hook_routes(self):
        for field in [getattr(self, name) for name in dir(self)]:
            route_rules = getattr(field, FlaskExt._HOOK_ROUTE_PROP, [])
            for route_rule in route_rules:
                super(FlaskExt, self).route(
                    *route_rule.args, **route_rule.kwargs)(field)

    def __init__(self, resource_name):
        super(FlaskExt, self).__init__(resource_name)
        self._hook_routes()


class WebServer(FlaskExt):
    """Wrapper for web-server functionality"""
    def __init__(self):
        super(WebServer, self).__init__(__name__)
        if (os.environ.get('WERKZEUG_RUN_MAIN') != 'true' and
                'gunicorn' not in os.environ.get('SERVER_SOFTWARE', '')):
            return  # skip any heavy operations for Werkzeug debug wrapper

        if SENTRY_URI:
            self.sentry = raven.contrib.flask.Sentry(self, dsn=SENTRY_URI)
        self.before_request(WebServer._redirect_to_https)
        self.before_request(self._check_auth)
        self.config['MONGO_URI'] = MONGO_URI
        self.mongo = flask_pymongo.PyMongo(self)
        with self.app_context() as ctx:
            self.tokens = store.TokenStore(self.mongo.db.tokens, ctx,
                                           key=CRYPTO_KEY)
            self.archive = slack_archive.SlackArchive(
                self.mongo, ctx, self.tokens, SLACK_TEAM_TOKEN)

    @staticmethod
    def start(wsgi_mode):
        app = WebServer()
        if not wsgi_mode:
            app.run(host='::', port=int(os.environ.get('PORT', 8080)),
                    ssl_context=('fullchain.pem', 'privkey.pem'), debug=True)
        return app

    @staticmethod
    def _is_forced_debug():
        return os.environ.get('DEBUG_SERVER', '0') == '1'

    @staticmethod
    def is_production():
        return __name__ == 'app' or WebServer._is_forced_debug()

    @staticmethod
    def url_for(endpoint):
        url = flask.url_for(endpoint, _external=True)
        if WebServer.is_production():
            url = url.replace('http://', 'https://', 1)
        return url

    @staticmethod
    def _redirect_page(url, msg):
        return flask.render_template('redirect.htm', url_to=url, message=msg)

    @staticmethod
    def _basic_page(title, html):
        return flask.render_template('basic.htm', title=title, html=html)

    @staticmethod
    def cookies_expire_date():
        """:returns: now plus one year date in cookie-expected time format"""
        return time.strftime("%a, %d-%b-%Y %T GMT",
                             time.gmtime(time.time() + 365 * 24 * 60 * 60))

    @FlaskExt.route('/<path:filename>')
    def send_file(self, filename):
        return flask.send_from_directory(self.static_folder, filename)

    @FlaskExt.route('/users')
    def users(self):
        domain = flask.request.args.get('domain')
        return self.archive.users_list(domain)

    @FlaskExt.route('/')
    def index(self):
        if self.tokens.is_known_user(flask.request.cookies.get('auth')):
            return flask.redirect('/browse', 302)
        return WebServer._redirect_page('/login', 'Auth required')

    @FlaskExt.route('/stat')
    def stat(self):
        stat = self.archive.stat()
        return WebServer._basic_page(
            'Statistics',
            '<div class="col-md-6">'
            ' <div class="panel panel-default" align="center">'
            '  <div class="panel-heading">'
            '   <h3 class="panel-title">Statistics</h3>'
            '  </div>'
            '  <table class="table">' +
            ''.join([
                '<tr><th>' +
                param.keys()[0] +
                '</th><td>' +
                str(param.values()[0]) +
                '</td></tr>'
                for param in stat]) +
            '  </table>'
            ' </div>'
            '</div>')

    @FlaskExt.route('/login')
    def login(self):
        if flask.request.args.get('code'):
            return self._login_oauth()
        # logging in is not in progress
        auth = '&redirect_uri=' + WebServer.url_for('login')
        return WebServer._basic_page(
            'Login',
            '<div class="jumbotron" align="center">'
            '  <h1>You have to authenticate first:</h1>'
            '  <a class="btn btn-default btn-lg" href="{}">'
            '    <img src="https://slack.com/favicon.ico" width="24"/>'
            '    Basic (public channels)'
            '  </a>'
            '  &nbsp;'
            '  <a class="btn btn-default btn-lg" href="{}">'
            '    <img src="https://slack.com/favicon.ico" width="24"/>'
            '    Advanced (import private messaging)'
            '  </a>'
            '</div>'.format(LOGIN_LINK+auth, TOKEN_LINK+auth)
        )

    @FlaskExt.route('/logout')
    def logout(self):
        enc_key = flask.request.cookies.get('auth')
        user_info = self.tokens[enc_key]
        response = flask.make_response(
            WebServer._redirect_page('https://slack.com', 'Bye'))
        self.mongo.db.z_logouts.insert_one({'_id': time.time(),
                                            'user': user_info['login']})
        response.delete_cookie('auth')
        del self.tokens[enc_key]
        return response

    @FlaskExt.route('/search')
    def search(self):
        """3 cases here: search everywhere/in stream/by context(message)"""
        user = self.tokens[flask.request.cookies.get('auth')]
        query = flask.request.args.get('q', '')
        stream = flask.request.args.get('s', '')
        context = flask.request.args.get('c', '')
        page = int(flask.request.args.get('p', 0))
        self.mongo.db.z_search.insert_one({'_id': time.time(),
                                           'user': user['login'],
                                           'q': query})
        results = []
        if query == '':
            return flask.render_template('search.htm', results=results)
        if stream != '' and not self.archive.has_stream_access(user, stream):
            return self.report_access_denied()
        if context != '':
            results, msg_count = self.archive.find_messages_around(
                context, stream, page)
        elif stream != '':
            results, msg_count = self.archive.find_messages_in_stream(
                query, stream, page)
        else:
            streams = self.archive.filter_streams(user, 'all')
            streams = streams[:-1]  # public/private/direct, skip filter name
            # chain tuple of lists to flat list
            streams_list = itertools.chain(*streams)
            stream_ids = [stream_item['_id'] for stream_item in streams_list]
            results, msg_count = self.archive.find_messages(
                query, stream_ids, page)
        return flask.render_template(
            'search.htm', results=results, total=msg_count, q=query,
            s=stream, c=context, p=page,
            n=slack_archive.MESSAGES_NUMBER_PER_SEARCH_REQUEST)

    def report_access_denied(self):
        user_info = self.tokens[flask.request.cookies.get('auth')]
        self.mongo.db.z_access.insert_one({'_id': time.time(),
                                           'user': user_info['login'],
                                           'page': flask.request.url})
        return WebServer._basic_page(
            'Access denied',
            '<div class="jumbotron" align="center">'
            '  <h1>Access denied</h1>'
            '</div>')

    @FlaskExt.route('/browse')
    def browse(self):
        user_info = self.tokens[flask.request.cookies.get('auth')]
        stream = flask.request.args.get('s', '')
        page = int(flask.request.args.get('p', 0))
        self.mongo.db.z_browse.insert_one({'_id': time.time(),
                                           'user': user_info['login'],
                                           's': stream})
        if stream == '':
            filter_name = flask.request.args.get('filter', 'my')
            public, private, direct, filter_name = self.archive.filter_streams(
                user_info, filter_name)
            return flask.render_template(
                'browse.htm', channels=public, groups=private, ims=direct,
                f=filter_name, advanced_user=user_info['full_access'])
        if not self.archive.has_stream_access(user_info, stream):
            return self.report_access_denied()
        results, streams_cnt = self.archive.stream_messages(stream, page)
        return flask.render_template(
            'stream.htm', results=results, total=streams_cnt, s=stream,
            p=page, n=slack_archive.MESSAGES_NUMBER_PER_STREAM_REQUEST)

    @staticmethod
    @FlaskExt.route('/import', methods=['GET', 'POST'])
    def upload():
        # TODO check admin rights here
        if WebServer.is_production():
            return WebServer._redirect_page('/browse', 'Access denied')
        archive = flask.request.files.get('archive')
        if archive and archive.filename.endswith('.zip'):
            archive.save(slack_archive.LOCAL_ARCHIVE_FILE)
            return WebServer._redirect_page('/import_db',
                                            archive.filename + ' saved')
        return WebServer._basic_page(
            'Archive upload',
            '<form action="" method="POST" enctype="multipart/form-data">'
            ' <div class="input-group input-group-lg col-md-7" align="center">'
            '  <span class="input-group-addon">Select .zip archive</span>'
            '   <input type="file" name="archive" class="form-control"/>'
            '   <span class="input-group-btn">'
            '    <input type="submit" class="btn btn-primary" value="Import"/>'
            '   </span>'
            '  </span>'
            ' </div>'
            '</form>')

    @FlaskExt.route('/import_db')
    def import_db(self):
        # TODO check admin rights here
        if WebServer.is_production():
            return WebServer._redirect_page('/browse', 'Access denied')
        result, types_new = self.archive.import_archive()
        return WebServer._basic_page('Archive import complete',
                                     'Import complete!<br />' +
                                     str(result) + '<br/>' +
                                     str(types_new))

    @staticmethod
    def _redirect_to_https():
        is_http = flask.request.is_secure or \
                  flask.request.headers.get('X-Forwarded-Proto') == 'http'
        if is_http and WebServer.is_production():
            url = flask.request.url.replace('http://', 'https://', 1)
            return flask.redirect(url, code=301)

    def _check_auth(self):
        if self.tokens.is_known_user(flask.request.cookies.get('auth')):
            return
        if flask.request.path in ['/login'] or \
           os.path.isfile(os.path.join(self.static_folder,
                                       flask.request.path[1:])):
            return
        return self._redirect_page('/login', 'Auth required')

    def _login_oauth(self):
        try:
            oauth = Slacker.oauth.access(
                client_id=SLACK_CLIENT_ID,
                client_secret=SLACK_CLIENT_SECRET,
                code=flask.request.args['code'],
                redirect_uri=WebServer.url_for('login')
            ).body
        except Error as err:
            self.mongo.db.z_errors.insert_one({'_id': time.time(),
                                               'ctx': 'oauth',
                                               'msg': str(err)})
            return WebServer._basic_page('OAuth error',
                                         'OAuth error: ' + str(err))
        token = oauth['access_token']
        identity_only = oauth['scope'].count(',') == 1
        return self._login_with_token(token, identity_only)

    def _login_with_token(self, token, identity_only):
        try:
            api_auth = Slacker(token).auth.test().body
            assert api_auth['team_id'] == SLACK_TEAM_ID
        except Error as err:
            self.mongo.db.z_errors.insert_one({'_id': time.time(),
                                               'ctx': 'auth.test',
                                               'msg': str(err)})
            return WebServer._basic_page('Auth error',
                                         'Auth error: ' + str(err))
        except AssertionError:
            return WebServer._basic_page('Wrong team',
                                         'Wrong team: ' + api_auth['team'])
        return self._login_success(token, api_auth, identity_only)

    def _login_success(self, token, api_user_info, identity_only):
        response = flask.redirect('/browse', 302)
        auth_key = self.tokens.upsert(token,
                                      user=api_user_info,
                                      full_access=not identity_only)
        self.mongo.db.z_logins.insert_one({'_id': time.time(),
                                           'user': api_user_info['user']})
        response.set_cookie('auth', auth_key,
                            expires=WebServer.cookies_expire_date())
        self.archive.streams_fetch(token)
        return response


class GUnicornRunner(gunicorn.app.base.BaseApplication):
    """GUnicorn wrapper to avoid pickling too much server logic"""
    def __init__(self):
        super(GUnicornRunner, self).__init__()

    def init(self, parser, opts, args):
        """We need to override this"""
        pass

    def load_config(self):
        """Default hardcoded config"""
        setup = {
            'bind': '[::]:{}'.format(int(os.environ.get('PORT', 8080))),
            'certfile': 'cert.pem',
            'keyfile': 'privkey.pem',
            'ca_certs': 'chain.pem',
            'workers': 8,
            'daemon': True,
            'timeout': 10*60,  # wait before results
            'graceful_timeout': 0,  # wait before kill after timeout
            'accesslog': '-',  # log to stderr
            'enable_stdio_inheritance': True,
        }
        for opt_key, opt_value in setup.items():
            self.cfg.set(opt_key, opt_value)

    def load(self):
        """Lazy init (called after fork)"""
        return WebServer.start(wsgi_mode=True)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--gunicorn':
        GUnicornRunner().run()
    elif len(sys.argv) > 1 and sys.argv[1] == '--flask':
        WebServer.start(wsgi_mode=False)
    else:
        print('Usage: {0} --gunicorn, {0} --flask'.format(sys.argv[0]))
