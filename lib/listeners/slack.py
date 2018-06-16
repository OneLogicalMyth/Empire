import base64
import random
import os
import re
import time
from datetime import datetime
import copy
import traceback
import sys
import json
from pydispatch import dispatcher
from slackclient import SlackClient

# Empire imports
from lib.common import helpers
from lib.common import agents
from lib.common import encryption
from lib.common import packets
from lib.common import messages


class Listener:

    def __init__(self, mainMenu, params=[]):

        self.info = {
            'Name': 'Slack',

            'Author': ['@OneLogicalMyth'],

            'Description': ("Starts a listener for Slack using the API."),

            # categories - client_server, peer_to_peer, broadcast, third_party
            'Category' : ('client_server'),

            'Comments': []
        }

        # any options needed by the stager, settable during runtime
        self.options = {
            # format:
            #   value_name : {description, required, default_value}

            'Name' : {
                'Description'   :   'Name for the listener.',
                'Required'      :   True,
                'Value'         :   'slack'
            },
            'APIToken' : {
                'Description'   :   'API Token, visit https://slack.com/apps/A0F7YS25R to get one.',
                'Required'      :   True,
                'Value'         :   'xoxb-123456789123-123456789123-ExampleSlackAPIToken'
            },
            'ChannelComms' : {
                'Description'   :   'The Slack channel to use for comms.',
                'Required'      :   True,
                'Value'         :   'empire_comms'
            },
            'PollInterval' : {
                'Description'   :   'How often to check Slack for new messages (Empire instance/server side). Recommended is 1 second.',
                'Required'      :   True,
                'Value'         :   1
            },
            'Launcher' : {
                'Description'   :   'Launcher string.',
                'Required'      :   True,
                'Value'         :   'powershell -noP -sta -w 1 -enc '
            },
            'StagingKey' : {
                'Description'   :   'Staging key for initial agent negotiation.',
                'Required'      :   True,
                'Value'         :   'ec1a0eab303df7f47caaed136561a960'
            },
            'DefaultDelay' : {
                'Description'   :   'Agent delay/reach back interval (in seconds).',
                'Required'      :   True,
                'Value'         :   5
            },
            'DefaultJitter' : {
                'Description'   :   'Jitter in agent reachback interval (0.0-1.0).',
                'Required'      :   True,
                'Value'         :   0.0
            },
            'DefaultLostLimit' : {
                'Description'   :   'Number of missed checkins before exiting',
                'Required'      :   True,
                'Value'         :   60
            },
            'DefaultProfile' : {
                'Description'   :   'Default communication profile for the agent.',
                'Required'      :   True,
                'Value'         :   "N/A|Slackbot 1.0(+https://api.slack.com/robots)"
            },
            'CertPath' : {
                'Description'   :   'Certificate path for https listeners.',
                'Required'      :   False,
                'Value'         :   ''
            },
            'KillDate' : {
                'Description'   :   'Date for the listener to exit (MM/dd/yyyy).',
                'Required'      :   False,
                'Value'         :   ''
            },
            'WorkingHours' : {
                'Description'   :   'Hours for the agent to operate (09:00-17:00).',
                'Required'      :   False,
                'Value'         :   ''
            },
            'Proxy' : {
                'Description'   :   'Proxy to use for request (default, none, or other).',
                'Required'      :   False,
                'Value'         :   'default'
            },
            'ProxyCreds' : {
                'Description'   :   'Proxy credentials ([domain\]username:password) to use for request (default, none, or other).',
                'Required'      :   False,
                'Value'         :   'default'
            },
            'SlackToken' : {
                'Description'   :   'Your SlackBot API token to communicate with your Slack instance.',
                'Required'      :   False,
                'Value'         :   ''
            },
            'SlackChannel' : {
                'Description'   :   'The Slack channel or DM that notifications will be sent to.',
                'Required'      :   False,
                'Value'         :   '#general'
            }
        }

        # required:
        self.mainMenu = mainMenu
        self.threads = {} # used to keep track of any threaded instances of this server

        # optional/specific for this module
        self.options['ChannelComms_ID'] = {
                                            'Description' : 'channel internal ID that slack uses',
                                            'Required'    : False,
                                            'Value'       : 'tbc'
                                          }

        # set the default staging key to the controller db default
        self.options['StagingKey']['Value'] = str(helpers.get_config('staging_key')[0])


    def default_response(self):
        """
        If there's a default response expected from the server that the client needs to ignore,
        (i.e. a default HTTP page), put the generation here.
        """
        print helpers.color("[!] default_response() not implemented for listeners/template")
        return ''


    def validate_options(self):
        """
        Validate all options for this listener.
        """

        for key in self.options:
            if self.options[key]['Required'] and (str(self.options[key]['Value']).strip() == ''):
                print helpers.color("[!] Option \"%s\" is required." % (key))
                return False

        # validate Slack API token and configuration
        sc = SlackClient(self.options['APIToken']['Value'])
        SlackChannels = sc.api_call('channels.list')

        # if the token is unable to retrieve the list of channels return exact error, most common is bad API token
        if 'error' in SlackChannels:
            print helpers.color('[!] An error was returned from Slack: ' + SlackChannels['error'])
            return False
        else:

            CommsName   = self.options['ChannelComms']['Value']

            # build a list of channel names and store the channel info for later use
            ChannelNames = []
            CommsChannel = None

            for channel in SlackChannels['channels']:
                ChannelNames.append(channel['name'])
                if CommsName == channel['name']:
                    CommsChannel = channel

            if not CommsName in ChannelNames or CommsChannel == None:
                print helpers.color('[!] No channel "' + CommsName + '", bots can\'t create channels so please fix manually.')
                return False
            elif CommsChannel['is_archived']:
                print helpers.color('[!] Channel "' + CommsName + '" is archived, bots can\'t unarchive channels so please fix manually.')
                return False
            elif not CommsChannel['is_member']:
                print helpers.color('[!] Bot is not a member of channel "' + CommsName + '", bots can\'t join channels so please fix manually.')
                return False

            self.options['ChannelComms_ID']['Value'] = CommsChannel['id']

        return True


    def generate_launcher(self, encode=True, obfuscate=False, obfuscationCommand="", userAgent='default', proxy='default', proxyCreds='default', stagerRetries='0', language=None, safeChecks='', listenerName=None):
        """
        Generate a basic launcher for the specified listener.
        """

        if not language:
            print helpers.color('[!] listeners/template generate_launcher(): no language specified!')
            return None

        if listenerName and (listenerName in self.mainMenu.listeners.activeListeners):

            # extract the set options for this instantiated listener
            listenerOptions = self.mainMenu.listeners.activeListeners[listenerName]['options']
            host = listenerOptions['Host']['Value']
            stagingKey = listenerOptions['StagingKey']['Value']
            profile = listenerOptions['DefaultProfile']['Value']
            uris = [a.strip('/') for a in profile.split('|')[0].split(',')]
            stage0 = random.choice(uris)
            launchURI = "%s/%s" % (host, stage0)

            if language.startswith('po'):
                # PowerShell
                return ''

            if language.startswith('py'):
                # Python
                return ''

            else:
                print helpers.color("[!] listeners/template generate_launcher(): invalid language specification: only 'powershell' and 'python' are current supported for this module.")

        else:
            print helpers.color("[!] listeners/template generate_launcher(): invalid listener name specification!")


    def generate_stager(self, listenerOptions, encode=False, encrypt=True, obfuscate=False, obfuscationCommand="", language=None):
        """
        If you want to support staging for the listener module, generate_stager must be
        implemented to return the stage1 key-negotiation stager code.
        """
        print helpers.color("[!] generate_stager() not implemented for listeners/template")
        return ''


    def generate_agent(self, listenerOptions, language=None, obfuscate=False, obfuscationCommand=""):
        """
        If you want to support staging for the listener module, generate_agent must be
        implemented to return the actual staged agent code.
        """
        print helpers.color("[!] generate_agent() not implemented for listeners/template")
        return ''


    def generate_comms(self, listenerOptions, language=None):
        """
        Generate just the agent communication code block needed for communications with this listener.
        This is so agents can easily be dynamically updated for the new listener.

        This should be implemented for the module.
        """

        if language:
            if language.lower() == 'powershell':

                updateServers = """
                    $Script:ControlServers = @("%s");
                    $Script:ServerIndex = 0;
                """ % (listenerOptions['Host']['Value'])

                getTask = """
                    $script:GetTask = {


                    }
                """

                sendMessage = """
                    $script:SendMessage = {
                        param($Packets)

                        if($Packets) {

                        }
                    }
                """

                return updateServers + getTask + sendMessage + "\n'New agent comms registered!'"

            elif language.lower() == 'python':
                # send_message()
                pass
            else:
                print helpers.color("[!] listeners/template generate_comms(): invalid language specification, only 'powershell' and 'python' are current supported for this module.")
        else:
            print helpers.color('[!] listeners/template generate_comms(): no language specified!')


    def start_server(self, listenerOptions):

        # utility function for handling commands
        def parse_commands(slack_events,bot_id):

            # Parses a list of events coming from the Slack RTM API to find commands.
            for event in slack_events:
                if event["type"] == "message" and not "subtype" in event:

                    # split format of {{AGENT_NAME}}:{{BASE64_RC4}}
                    if ':' in event["text"] and not event["user"] == bot_id:
                        agent, message = event["text"].split(':')
                        return agent, message

            return None, None

        # utility functions for handling Empire
        def upload_launcher():
            pass

        def upload_stager():
            pass

        def handle_stager():
            pass

        listener_options = copy.deepcopy(listenerOptions)

        listener_name = listener_options['Name']['Value']
        staging_key = listener_options['StagingKey']['Value']
        poll_interval = listener_options['PollInterval']['Value']
        api_token = listener_options['APIToken']['Value']
        channel_id = listener_options['ChannelComms_ID']['Value']

        slack_client = SlackClient(api_token)

        if slack_client.rtm_connect(with_team_state=False,auto_reconnect=True):

            # Read bot's user ID by calling Web API method `auth.test`
            bot_id = slack_client.api_call("auth.test")["user_id"]
            slack_client.api_call(
                "chat.postMessage",
                channel=channel_id,
                as_user=True,
                text='An Empire listener for slack has started. :raised_hands:'
            )
            
            # Set the listener in a while loop
            while True:

                # sleep for poll interval
                time.sleep(int(poll_interval))

                # try to process command sent if fails then simply wait until next poll interval and try again
                try:
                    agent, message = parse_commands(slack_client.rtm_read(),bot_id)
                    if message:
                        slack_client.api_call(
                            "chat.postMessage",
                            channel=channel_id,
                            as_user=True,
                            text='Not implemented.'
                        )
                        print helpers.color('[!] Not implemented... message from "{}": {}'.format(agent,message))
                   
                except Exception as e:
                    print helpers.color("[!] The command '" + str(command) + "' was sent by '" + str(user) + "' but failed. Exception is '" + str(e) + "'")
                
        else:
            print helpers.color("[!] Connection failed. Exception printed above.")
        


    def start(self, name=''):
        """
        If a server component needs to be started, implement the kick off logic
        here and the actual server code in another function to facilitate threading
        (i.e. start_server() in the http listener).
        """

        listenerOptions = self.options
        if name and name != '':
            self.threads[name] = helpers.KThread(target=self.start_server, args=(listenerOptions,))
            self.threads[name].start()
            time.sleep(1)
            # returns True if the listener successfully started, false otherwise
            return self.threads[name].is_alive()
        else:
            name = listenerOptions['Name']['Value']
            self.threads[name] = helpers.KThread(target=self.start_server, args=(listenerOptions,))
            self.threads[name].start()
            time.sleep(1)
            # returns True if the listener successfully started, false otherwise
            return self.threads[name].is_alive()

        return True


    def shutdown(self, name=''):
        """
        If a server component was started, implement the logic that kills the particular
        named listener here.
        """

        if name and name != '':
            print helpers.color("[!] Killing listener '%s'" % (name))
            self.threads[name].kill()
        else:
            print helpers.color("[!] Killing listener '%s'" % (self.options['Name']['Value']))
            self.threads[self.options['Name']['Value']].kill()

        pass
