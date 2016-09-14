from twisted.internet.interfaces import IReactorCore
from twisted.internet.interfaces import IListeningPort
from twisted.internet.address import IPv4Address
from twisted.internet import defer, error, task, tcp
from twisted.python.failure import Failure
from twisted.trial import unittest
from twisted.test import proto_helpers

from txtorcon import Tor
from txtorcon import TorControlProtocol
from txtorcon import launch

from zope.interface import implementer

import functools
from mock import Mock, patch
from StringIO import StringIO


class FakeProcessTransport(proto_helpers.StringTransportWithDisconnection):
    pid = -1

    def signalProcess(self, signame):
        self.process_protocol.processEnded(
            Failure(error.ProcessTerminated(signal=signame))
        )
    def closeStdin(self):
        return


class FakeProcessTransportNeverBootstraps(FakeProcessTransport):

    pid = -1

    def closeStdin(self):
        return


@implementer(IListeningPort)
class FakePort(object):
    def __init__(self, port):
        self._port = port

    def startListening(self):
        pass

    def stopListening(self):
        pass

    def getHost(self):
        return IPv4Address('TCP', "127.0.0.1", self._port)


@implementer(IReactorCore)
class FakeReactor(task.Clock):

    def __init__(self, test, trans, on_protocol, listen_ports=[]):
        super(FakeReactor, self).__init__()
        self.test = test
        self.transport = trans
        self.on_protocol = on_protocol
        self.listen_ports = listen_ports
        # util.available_tcp_port ends up 'asking' for free ports via
        # listenTCP, ultimately, and the answers we send back are from
        # this list

    def spawnProcess(self, processprotocol, bin, args, env, path,
                     uid=None, gid=None, usePTY=None, childFDs=None):
        self.protocol = processprotocol
        self.protocol.makeConnection(self.transport)
        self.transport.process_protocol = processprotocol
        self.on_protocol(self.protocol)
        return self.transport

    def addSystemEventTrigger(self, *args):
        self.test.assertEqual(args[0], 'before')
        self.test.assertEqual(args[1], 'shutdown')
        # we know this is just for the temporary file cleanup, so we
        # nuke it right away to avoid polluting /tmp by calling the
        # callback now.
        args[2]()

    def removeSystemEventTrigger(self, id):
        pass

    def listenTCP(self, *args, **kw):
        port = self.listen_ports.pop()
        return FakePort(port)


class LaunchTorTests(unittest.TestCase):

    @defer.inlineCallbacks
    def test_launch_fails(self):
        trans = FakeProcessTransport()
        reactor = FakeReactor(self, trans, lambda p: None)

        try:
            tor = yield launch(reactor)
            self.fail("Should fail")
        except Exception as e:
            print("XXXX", e)       

    @defer.inlineCallbacks
    def test_launch_no_ireactorcore(self):
        try:
            yield launch(None)
            self.fail("should get exception")
        except ValueError as e:
            self.assertTrue("provide IReactorCore" in str(e))

    @patch('txtorcon.controller.TorProcessProtocol')
    @defer.inlineCallbacks
    def test_successful_launch(self, tpp):
        trans = FakeProcessTransport()
        reactor = FakeReactor(self, trans, lambda p: None, [1, 2, 3])

        def foo(*args, **kw):
            cb = kw['connected_cb']
            cb.callback('foo')
            return Mock()
        tpp.side_effect=foo

        tor = yield launch(reactor)
        self.assertTrue(isinstance(tor, Tor))

    @patch('txtorcon.controller.sys')
    @patch('txtorcon.controller.pwd')
    @patch('txtorcon.controller.os.geteuid')
    @patch('txtorcon.controller.os.chown')
    def XXXtest_launch_root_changes_tmp_ownership(self, chown, euid, _pwd, _sys):
        print("FIXME code is if False:'d away")
        return
        _pwd.return_value = 1000
        _sys.platform = 'linux2'
        euid.return_value = 0
        config = TorConfig()
        config.User = 'chuffington'
        d = launch(Mock(), tor_binary='/bin/echo')
        self.assertEqual(1, chown.call_count)

    def test_launch_timeout_exception(self):
        """
        we provide a timeout, and it expires
        """
        trans = Mock()
        trans.signalProcess = Mock(side_effect=error.ProcessExitedAlready)
        trans.loseConnection = Mock()
        react = FakeReactor(self, trans, Mock(), [1234])
        d = launch(
            reactor=react,
            tor_binary='/bin/echo',
            socks_port=1234,
            timeout=10,
        )
        react.advance(12)
        self.assertTrue(trans.loseConnection.called)

    @defer.inlineCallbacks
    def test_launch_timeout_process_exits(self):
        # cover the "one more edge case" where we get a processEnded()
        # but we've already "done" a timeout.
        trans = Mock()
        trans.signalProcess = Mock()
        trans.loseConnection = Mock()

        class MyFakeReactor(FakeReactor):
            def spawnProcess(self, processprotocol, bin, args, env, path,
                             uid=None, gid=None, usePTY=None, childFDs=None):
                self.protocol = processprotocol
                self.protocol.makeConnection(self.transport)
                self.transport.process_protocol = processprotocol
                self.on_protocol(self.protocol)

                status = Mock()
                status.value.exitCode = None
                processprotocol.processEnded(status)
                return self.transport

        react=MyFakeReactor(self, trans, Mock(), [1234, 9052])

        d = launch(
            reactor=react,
            tor_binary='/bin/echo',
            timeout=10,
            data_directory='/dev/null',
        )
        react.advance(20)

        try:
            yield d
        except RuntimeError as e:
            self.assertTrue("Tor was killed" in str(e))

        errs = self.flushLoggedErrors(RuntimeError)
        self.assertEqual(1, len(errs))
        self.assertTrue("Tor was killed" in str(errs[0]))

    @defer.inlineCallbacks
    def test_launch_wrong_stdout(self):
        try:
            yield launch(
                FakeReactor(self, Mock(), Mock()),
                stdout=object(),
                tor_binary='/bin/echo',
            )
            self.fail("Should have thrown an error")
        except RuntimeError as e:
            self.assertTrue("file-like object needed" in str(e).lower())

    @defer.inlineCallbacks
    def test_launch_with_timeout(self):
        # XXX not entirely sure what this was/is supposed to be
        # testing, but it covers an extra 7 lines of code??
        timeout = 5

        def connector(proto, trans):
            proto._set_valid_events('STATUS_CLIENT')
            proto.makeConnection(trans)
            proto.post_bootstrap.callback(proto)
            return proto.post_bootstrap

        class OnProgress:
            def __init__(self, test, expected):
                self.test = test
                self.expected = expected

            def __call__(self, percent, tag, summary):
                self.test.assertEqual(
                    self.expected[0],
                    (percent, tag, summary)
                )
                self.expected = self.expected[1:]
                self.test.assertTrue('"' not in summary)
                self.test.assertTrue(percent >= 0 and percent <= 100)

        def on_protocol(proto):
            proto.outReceived('Bootstrapped 100%\n')

        trans = FakeProcessTransportNeverBootstraps()
#        trans.protocol = self.protocol
        creator = functools.partial(connector, Mock(), Mock())
        react = FakeReactor(self, trans, on_protocol, [1234, 9052])
        d = launch(react, connection_creator=creator,
                   timeout=timeout, tor_binary='/bin/echo')
        # FakeReactor is a task.Clock subclass and +1 just to be sure
        react.advance(timeout + 1)

        try:
            res = yield d
            self.fail()
        except RuntimeError as e:
            print("BOOM", e)
            self.assertTrue(
                'Tor was killed (TERM).' in str(e)
            )
        # could/should just use return from this to do asserts?
        self.flushLoggedErrors(RuntimeError)

    @defer.inlineCallbacks
    def XXXtest_launch_with_timeout_that_doesnt_expire(self):
        config = TorConfig()
        config.OrPort = 1234
        config.SocksPort = 9999
        timeout = 5

        def connector(proto, trans):
            proto._set_valid_events('STATUS_CLIENT')
            proto.makeConnection(trans)
            proto.post_bootstrap.callback(proto)
            return proto.post_bootstrap

        class OnProgress:
            def __init__(self, test, expected):
                self.test = test
                self.expected = expected

            def __call__(self, percent, tag, summary):
                self.test.assertEqual(
                    self.expected[0],
                    (percent, tag, summary)
                )
                self.expected = self.expected[1:]
                self.test.assertTrue('"' not in summary)
                self.test.assertTrue(percent >= 0 and percent <= 100)

        def on_protocol(proto):
            proto.outReceived('Bootstrapped 100%\n')

        trans = FakeProcessTransport()
        trans.protocol = self.protocol
        creator = functools.partial(connector, self.protocol, self.transport)
        react = FakeReactor(self, trans, on_protocol, [1234, 9052])
        d = launch(react, connection_creator=creator,
                   timeout=timeout, tor_binary='/bin/echo')
        # FakeReactor is a task.Clock subclass and +1 just to be sure
        react.advance(timeout + 1)

        res = yield d
        self.assertTrue(res.protocol == self.protocol)

    @defer.inlineCallbacks
    def test_tor_produces_stderr_output(self):
        def connector(proto, trans):
            proto._set_valid_events('STATUS_CLIENT')
            proto.makeConnection(trans)
            proto.post_bootstrap.callback(proto)
            return proto.post_bootstrap

        def on_protocol(proto):
            proto.errReceived('Something went horribly wrong!\n')

        trans = FakeProcessTransport()
        trans.protocol = Mock()
        fakeout = StringIO()
        fakeerr = StringIO()
        creator = functools.partial(connector, Mock(), Mock())
        try:
            res = yield launch(
                FakeReactor(self, trans, on_protocol, [1234, 9052]),
                connection_creator=creator,
                tor_binary='/bin/echo',
                stdout=fakeout,
                stderr=fakeerr,
            )
            self.fail()  # should't get callback
        except RuntimeError as e:
            self.assertEqual('', fakeout.getvalue())
            self.assertEqual('Something went horribly wrong!\n', fakeerr.getvalue())
            self.assertTrue(
                'Something went horribly wrong!' in str(e)
            )

    def _test_tor_connection_fails(self):
        """
        We fail to connect once, and then successfully connect --
        testing whether we're retrying properly on each Bootstrapped
        line from stdout.
        """

        config = TorConfig()
        config.OrPort = 1234
        config.SocksPort = 9999

        class Connector:
            count = 0

            def __call__(self, proto, trans):
                self.count += 1
                if self.count < 2:
                    return defer.fail(
                        error.CannotListenError(None, None, None)
                    )

                proto._set_valid_events('STATUS_CLIENT')
                proto.makeConnection(trans)
                proto.post_bootstrap.callback(proto)
                return proto.post_bootstrap

        def on_protocol(proto):
            proto.outReceived('Bootstrapped 90%\n')

        trans = FakeProcessTransport()
        trans.protocol = self.protocol
        creator = functools.partial(Connector(), self.protocol, self.transport)
        d = launch(
            FakeReactor(self, trans, on_protocol, [1234, 9052]),
            connection_creator=creator,
            tor_binary='/bin/echo'
        )
        d.addCallback(self.setup_complete_fails)
        return self.assertFailure(d, Exception)

    def _test_tor_connection_user_data_dir(self):
        """
        Test that we don't delete a user-supplied data directory.
        """

        config = TorConfig()
        config.OrPort = 1234

        class Connector:
            def __call__(self, proto, trans):
                proto._set_valid_events('STATUS_CLIENT')
                proto.makeConnection(trans)
                proto.post_bootstrap.callback(proto)
                return proto.post_bootstrap

        def on_protocol(proto):
            proto.outReceived('Bootstrapped 90%\n')

        my_dir = tempfile.mkdtemp(prefix='tortmp')
        config.DataDirectory = my_dir
        trans = FakeProcessTransport()
        trans.protocol = self.protocol
        creator = functools.partial(Connector(), self.protocol, self.transport)
        d = launch(
            FakeReactor(self, trans, on_protocol, [1234, 9051]),
            connection_creator=creator,
            tor_binary='/bin/echo'
        )

        def still_have_data_dir(tor, tester):
            tor._process_protocol.cleanup()  # FIXME? not really unit-testy as this is sort of internal function
            tester.assertTrue(os.path.exists(my_dir))
            delete_file_or_tree(my_dir)

        d.addCallback(still_have_data_dir, self)
        d.addErrback(self.fail)
        return d

    def _test_tor_connection_user_control_port(self):
        """
        Confirm we use a user-supplied control-port properly
        """

        config = TorConfig()
        config.OrPort = 1234
        config.ControlPort = 4321

        class Connector:
            def __call__(self, proto, trans):
                proto._set_valid_events('STATUS_CLIENT')
                proto.makeConnection(trans)
                proto.post_bootstrap.callback(proto)
                return proto.post_bootstrap

        def on_protocol(proto):
            proto.outReceived('Bootstrapped 90%\n')
            proto.outReceived('Bootstrapped 100%\n')

        trans = FakeProcessTransport()
        trans.protocol = self.protocol
        creator = functools.partial(Connector(), self.protocol, self.transport)
        d = launch(
            FakeReactor(self, trans, on_protocol, [9052]),
            connection_creator=creator,
            tor_binary='/bin/echo',
            socks_port=1234,
        )

        def check_control_port(proto, tester):
            # we just want to ensure launch() didn't mess with
            # the controlport we set
            tester.assertEquals(config.ControlPort, 4321)

        d.addCallback(check_control_port, self)
        d.addErrback(self.fail)
        return d

    @defer.inlineCallbacks
    def _test_tor_connection_default_control_port(self):
        """
        Confirm a default control-port is set if not user-supplied.
        """

        class Connector:
            def __call__(self, proto, trans):
                proto._set_valid_events('STATUS_CLIENT')
                proto.makeConnection(trans)
                proto.post_bootstrap.callback(proto)
                return proto.post_bootstrap

        def on_protocol(proto):
            proto.outReceived('Bootstrapped 90%\n')
            proto.outReceived('Bootstrapped 100%\n')

        trans = FakeProcessTransport()
        trans.protocol = self.protocol
        creator = functools.partial(Connector(), self.protocol, self.transport)
        tor = yield launch(
            FakeReactor(self, trans, on_protocol, [9052]),
            connection_creator=creator,
            tor_binary='/bin/echo',
            socks_port=1234,
        )

        self.assertEqual(tor.config.ControlPort, 9052)

    def XXXtest_progress_updates(self):
        self.got_progress = False

        def confirm_progress(p, t, s):
            self.assertEqual(p, 10)
            self.assertEqual(t, 'tag')
            self.assertEqual(s, 'summary')
            self.got_progress = True
        process = TorProcessProtocol(None, confirm_progress)
        process.progress(10, 'tag', 'summary')
        self.assertTrue(self.got_progress)

    def XXXtest_quit_process(self):
        process = TorProcessProtocol(None)
        process.transport = Mock()

        d = process.quit()
        self.assertFalse(d.called)

        process.processExited(Failure(error.ProcessTerminated(exitCode=15)))
        self.assertTrue(d.called)
        process.processEnded(Failure(error.ProcessDone(None)))
        self.assertTrue(d.called)
        errs = self.flushLoggedErrors()
        self.assertEqual(1, len(errs))
        self.assertTrue("Tor exited with error-code" in str(errs[0]))

    def XXXtest_quit_process_already(self):
        process = TorProcessProtocol(None)
        process.transport = Mock()

        def boom(sig):
            self.assertEqual(sig, 'TERM')
            raise error.ProcessExitedAlready()
        process.transport.signalProcess = Mock(side_effect=boom)

        d = process.quit()
        process.processEnded(Failure(error.ProcessDone(None)))
        self.assertTrue(d.called)
        errs = self.flushLoggedErrors()
        self.assertEqual(1, len(errs))
        self.assertTrue("Tor exited with error-code" in str(errs[0]))

    def XXXtest_status_updates(self):
        process = TorProcessProtocol(None)
        process.status_client("NOTICE CONSENSUS_ARRIVED")

    def XXXtest_tor_launch_success_then_shutdown(self):
        """
        There was an error where we double-callbacked a deferred,
        i.e. success and then shutdown. This repeats it.
        """
        process = TorProcessProtocol(None)
        process.status_client(
            'STATUS_CLIENT BOOTSTRAP PROGRESS=100 TAG=foo SUMMARY=cabbage'
        )
        # XXX why this assert?
        self.assertEqual(None, process._connected_cb)

        class Value(object):
            exitCode = 123

        class Status(object):
            value = Value()
        process.processEnded(Status())
        self.assertEquals(len(self.flushLoggedErrors(RuntimeError)), 1)

    @defer.inlineCallbacks
    def XXXtest_launch_no_control_port(self):
        '''
        See Issue #80. This allows you to launch tor with a TorConfig
        with ControlPort=0 in case you don't want a control connection
        at all. In this case you get back a TorProcessProtocol and you
        own both pieces. (i.e. you have to kill it yourself).
        '''
        print("skipping; don't support controlport=0 I guess?")
        return

        config = TorConfig()
        config.ControlPort = 0
        trans = FakeProcessTransportNoProtocol()
        trans.protocol = self.protocol

        def creator(*args, **kw):
            print("Bad: connection creator called")
            self.fail()

        def on_protocol(proto):
            self.process_proto = proto
        tor_d = launch(
            reactor=FakeReactor(self, trans, on_protocol, [9052]),
            connection_creator=creator,
            tor_binary='/bin/echo',
            socks_port=1234,
        )
        res = yield tor_d
        self.assertEqual(res._process_protocol, self.process_proto)


class IteratorTests(unittest.TestCase):
    def XXXtest_iterate_torconfig(self):
        cfg = TorConfig()
        cfg.FooBar = 'quux'
        cfg.save()
        cfg.Quux = 'blimblam'

        keys = sorted([k for k in cfg])

        self.assertEqual(['FooBar', 'Quux'], keys)


