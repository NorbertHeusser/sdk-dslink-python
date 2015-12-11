import argparse
import base64
import hashlib
import json
import logging
import os.path
from urlparse import urlparse
from twisted.internet import reactor

from dslink.Crypto import Keypair
from dslink.Handshake import Handshake
from dslink.Node import Node
from dslink.Profile import ProfileManager
from dslink.Requester import Requester
from dslink.WebSocket import WebSocket


class DSLink:
    """
    Base DSLink class which creates the node structure,
    subscription/stream manager, and connects to the broker.
    """

    def __init__(self, config):
        """
        Construct for DSLink.
        :param config: Configuration object.
        """
        self.active = False
        self.nodes_changed = False
        self.needs_auth = False

        # DSLink Configuration
        self.config = config
        self.server_config = None

        # Logger setup
        self.logger = self.create_logger("DSLink", self.config.log_level)
        self.logger.info("Starting DSLink")

        # Load or create an empty Node structure
        self.super_root = self.load_nodes()
        self.create_defs()

        # Managers setup
        self.subman = LocalSubscriptionManager()
        self.strman = StreamManager()
        self.profile_manager = ProfileManager(self)
        if self.config.requester:
            self.requester = Requester(self)

        # DSLink setup
        self.keypair = Keypair(self.config.keypair_path)
        self.handshake = Handshake(self, self.keypair)
        self.handshake.run_handshake()
        self.dsid = self.handshake.get_dsid()

        # Connection setup
        self.wsp = None
        self.websocket = WebSocket(self)

        # Start saving timer
        if not self.config.no_save_nodes:
            reactor.callLater(1, self.save_timer)

        reactor.callLater(1, self.start)

        self.logger.info("Started DSLink")
        self.logger.debug("Starting reactor")
        reactor.run()

    # noinspection PyBroadException
    def load_nodes(self):
        """
        Load nodes.json file from disk, use backup if necessary. If that fails, then reset to defaults.
        """
        if os.path.exists(self.config.nodes_path):
            try:
                nodes_file = open(self.config.nodes_path, "r")
                obj = json.load(nodes_file)
                nodes_file.close()
                return Node.from_json(obj, None, "", link=self)
            except Exception, e:
                print(e)
                self.logger.error("Unable to load nodes data")
                if os.path.exists(self.config.nodes_path + ".bak"):
                    try:
                        self.logger.warn("Restoring backup nodes")
                        os.remove(self.config.nodes_path)
                        os.rename(self.config.nodes_path + ".bak", self.config.nodes_path)
                        nodes_file = open(self.config.nodes_path, "r")
                        obj = json.load(nodes_file)
                        nodes_file.close()
                        return Node.from_json(obj, None, "", link=self)
                    except:
                        self.logger.error("Unable to restore nodes, using default")
                        return self.get_default_nodes()
                else:
                    self.logger.warn("Backup nodes data doesn't exist, using default")
                    return self.get_default_nodes()
        else:
            return self.get_default_nodes()

    def save_timer(self):
        """
        Save timer, called every 5 seconds by default.
        """
        self.save_nodes()
        # Call again later...
        reactor.callLater(5, self.save_timer)

    def save_nodes(self):
        """
        Save the nodes.json out to disk if changed, and create the bak file.
        """
        if self.nodes_changed:
            if os.path.exists(self.config.nodes_path + ".bak"):
                os.remove(self.config.nodes_path + ".bak")
            if os.path.exists(self.config.nodes_path):
                os.rename(self.config.nodes_path, self.config.nodes_path + ".bak")
            nodes_file = open(self.config.nodes_path, "w")
            nodes_file.write(json.dumps(self.super_root.to_json(), sort_keys=True, indent=2))
            nodes_file.flush()
            os.fsync(nodes_file.fileno())
            nodes_file.close()
            self.nodes_changed = False

    def start(self):
        """
        Called once the DSLink is initialized and connected.
        Override this rather than the constructor.
        """
        # Do nothing.
        self.logger.log("Running default init")

    # noinspection PyMethodMayBeStatic
    def get_default_nodes(self):
        """
        Create the default Node structure in this, override it.
        :return:
        """
        return self.get_root_node()

    def get_root_node(self):
        """
        Gets the default root Node. For use in get_default_nodes *ONLY*.
        :return: Default root Node.
        """
        root = Node("", None)
        root.link = self

        return root

    def create_defs(self):
        defs = Node("defs", self.super_root)
        defs.set_transient(True)
        defs.set_config("$hidden", True)
        defs.add_child(Node("profile", defs))
        self.super_root.add_child(defs)

    def get_auth(self):
        auth = str(self.server_config["salt"]) + self.shared_secret
        auth = base64.urlsafe_b64encode(hashlib.sha256(auth).digest()).decode("utf-8").replace("=", "")
        return auth

    def get_url(self):
        websocket_uri = self.config.broker[:-5].replace("http", "ws") + "/ws?dsId=%s" % self.dsid
        if self.needs_auth:
            websocket_uri += "&auth=%s" % self.get_auth()
        if self.config.token is not None:
            websocket_uri += "&token=%s" % self.config.token
        url = urlparse(websocket_uri)
        if url.port is None:
            port = 80
        else:
            port = url.port
        return websocket_uri, url, port

    @staticmethod
    def create_logger(name, log_level=logging.INFO):
        """
        Create a logger with the specified name.
        :param name: Logger name.
        :param log_level: Output Logger level.
        :return: Logger instance.
        """
        # Logger setup
        formatter = logging.Formatter('%(asctime)s %(name)s %(levelname)s %(message)s')
        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        ch.setLevel(log_level)
        logger = logging.getLogger(name)
        logger.setLevel(log_level)
        logger.addHandler(ch)
        return logger

    @staticmethod
    def add_padding(string):
        """
        Add padding to a URL safe base64 string.
        :param string:
        :return:
        """
        while len(string) % 4 != 0:
            string += "="
        return string


class LocalSubscriptionManager:
    """
    Manages subscriptions to Nodes.
    """

    def __init__(self):
        """
        Constructor of SubscriptionManager.
        """
        self.subscriptions = {}

    def subscribe(self, node, sid):
        """
        Store a Subscription to a Node.
        :param node: Node to subscribe to.
        :param sid: SID of Subscription.
        """
        self.subscriptions[sid] = node
        self.subscriptions[sid].add_subscriber(sid)

    def unsubscribe(self, sid):
        """
        Remove a Subscription to a Node.
        :param sid: SID of Subscription.
        """
        try:
            self.subscriptions[sid].remove_subscriber(sid)
            del self.subscriptions[sid]
        except KeyError:
            logging.getLogger("DSlink").debug("Unknown sid %s" % sid)


class StreamManager:
    """
    Manages streams for Nodes.
    """

    def __init__(self):
        """
        Constructor of StreamManager.
        """
        self.streams = {}

    def open_stream(self, node, rid):
        """
        Open a Stream.
        :param node: Node to handle streaming.
        :param rid: RID of Stream.
        """
        self.streams[rid] = node
        self.streams[rid].streams.append(rid)

    def close_stream(self, rid):
        """
        Close a Stream.
        :param rid: RID of Stream.
        """
        try:
            self.streams[rid].streams.remove(rid)
            del self.streams[rid]
        except KeyError:
            logging.getLogger("DSLink").debug("Unknown rid %s" % rid)


class Configuration:
    """
    Provides configuration to the DSLink.
    """

    def __init__(self, name, responder=False, requester=False, ping_time=30, keypair_path=".keys",
                 nodes_path="nodes.json", no_save_nodes=False):
        """
        Object that contains configuration for the DSLink.
        :param name: DSLink name.
        :param responder: True if acts as responder, default is False.
        :param requester: True if acts as requester, default is False.
        :param ping_time: Time between pings, default is 30.
        """
        if not responder and not requester:
            raise ValueError("DSLink is neither responder nor requester. Exiting now.")
        parser = argparse.ArgumentParser()
        parser.add_argument("--broker", default="http://localhost:8080/conn")
        parser.add_argument("--log", default="info")
        parser.add_argument("--token")
        args = parser.parse_args()
        self.name = name
        self.broker = args.broker
        self.log_level = args.log.lower()
        self.token = args.token
        self.responder = responder
        self.requester = requester
        self.ping_time = ping_time
        self.keypair_path = keypair_path
        self.nodes_path = nodes_path
        self.no_save_nodes = no_save_nodes

        if self.log_level == "critical":
            self.log_level = logging.CRITICAL
        elif self.log_level == "error":
            self.log_level = logging.ERROR
        elif self.log_level == "warning":
            self.log_level = logging.WARNING
        elif self.log_level == "info":
            self.log_level = logging.INFO
        elif self.log_level == "debug":
            self.log_level = logging.DEBUG
        elif self.log_level == "none":
            self.log_level = logging.NOTSET
