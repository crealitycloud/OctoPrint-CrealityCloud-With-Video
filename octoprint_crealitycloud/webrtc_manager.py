from aiortc import RTCIceCandidate, RTCSessionDescription, RTCConfiguration, RTCIceServer, VideoStreamTrack, RTCRtpSender, RTCPeerConnection
#from aiortc.contrib.media import MediaPlayer, MediaBlackhole
from .media_handlers import MediaPlayer, MediaBlackhole
from .recorder import Recorder
import platform
import requests
import json
import sys
import time
import logging
import os
sys.path.append('../../src/aiortc')
from octoprint.util import RepeatedTimer

class WebrtcManager():

    def __init__(self,devuceName, our_peer_id, options, close_queue, token, region, recorder, verbose=False):
        self._logger = logging.getLogger("octoprint.plugins.crealitycloud")
        self.our_peer_id = our_peer_id
        self.peers = {}
        self.options = options
        self.verbose = verbose
        self.deviceName = devuceName
        self.urls = ""
        self.username = ""
        self.credential = ""
        self.token = token
        self.websockets_client = None
        self.close_queue = close_queue
        self.region = region
        self.count = 0
        self.recorder = recorder
        self.filepath = ""
        # self.iceServers = [
        #     {"urls": "stun:stun.l.google.com:19302"},
        #     {"urls": "stun:stun1.l.google.com:19302"},
        #     {"urls": "stun:stun2.l.google.com:19302"}]
        # result = self.getIceServers(self.deviceName)
        # iceServers = result["result"]["iceServers"][0]
        # if iceServers is not None:
        #      self.urls = iceServers["urls"]
        #      self.username = iceServers["username"]
        #      self.credential = iceServers["credential"]
        # iceServers = {"urls": self.urls, "username": self.username, "credential":self.credential}
        # self._logger.info("iceServer:" + str(iceServers))
        # self.iceServers.append(iceServers)

    async def signaling_message_handler(self, ws, message):
        dict = json.loads(message)
        action = dict["action"]
        self.websockets_client = ws
        # elif(action == "bye"):
        #     peerId = data["from"]
        #     await self.remove_peer(self.peers[peerId])
        
        if(action == "ice_msg"):
            sdpMessage = dict["sdpMessage"]
            type = sdpMessage["type"]    
            if(type == "offer"):
                sdp = sdpMessage["data"]
                peerId = dict["from"]
                iceServer = dict["iceServers"]
                self._logger.info("iceServers:" + str(iceServer))
                if iceServer is not None:
                    self.urls = iceServer[0]["urls"]
                    self.username = iceServer[0]["username"]
                    self.credential = iceServer[0]["credential"]
                if 'media' in sdpMessage:
                    media = sdpMessage["media"]
                    self.filepath = self.recorder.find_video(media)
                    if os.access(self.filepath,os.F_OK):
                        self._logger.info("filepath:" + self.filepath)
                    else:
                        self._logger.info("filepath not exist")
                        return
                else:
                    media = ""
                if peerId in self.peers:
                    await self.remove_peer(peerId)
                await self.add_peer(peerId, "admin", True, media)
                self.track_states(self.peers[peerId])
                await self.update_session_description(self.peers[peerId], sdp, peerId)
            elif(type == "candidate"): 
                candidateMap = sdpMessage["data"]
                peerId = dict["from"]                
                if peerId in self.peers:
                    await self.update_ice_candidate(self.peers[peerId], candidateMap)
        elif(action =="push_online"):
            iceServer = dict["iceServers"]
            if iceServer is not None:
                self.urls = iceServer[0]["urls"]
                self.username = iceServer[0]["username"]
                self.credential = iceServer[0]["credential"]
                    
    async def signaling_disconnect_handler(self):
        # make a copy of all regisered peer ids since remove_peers deletes the peer and changes the dict to change which ruins the for loop
        peer_ids = list(self.peers.keys())
        for peer_id in peer_ids:
            await self.remove_peer(self.peers[peer_id])           

    async def add_peer(self, peer_id, peer_type, polite, media):
        if(peer_id in self.peers):
            if(self.verbose):
                self._logger.info("A peer connection with id", peer_id, "already exists")
        else:
            iceServers = []
            iceServer = {"urls": self.urls, "username": self.username, "credential":self.credential}
            self._logger.info("iceServer:" + str(iceServer))
            iceServers.append(iceServer)
            config = RTCConfiguration(
                [RTCIceServer(server.get("urls"), server.get("username"), server.get("credential")) for server in iceServers])
            pc = RTCPeerConnection(config)
            pc_states = None
            self.peers[peer_id] = {
                "peerId": peer_id,
                "peerType": peer_type,
                "polite": polite,
                "rtcPeerConnection": pc,
                "pushState": pc_states,
                "dataChannel": None,
                "localStream": None,
                "localVideoStream": None,
                "localAudioStream": None,
                "remoteStream": None,
                "remoteVideoStream": None,
                "remoteAudioStream": None,
                "makingOffer": False,
                "ignoreOffer": False,
                "isSettingRemoteAnswerPending": False,
                "sdp":None,
                "type":None,
                "mediaplayer":None,
                "media":media,
            }

            if(self.options.get("enableDataChannel")):
                self.peers[peer_id]["dataChannel"] = self.peers[peer_id]['rtcPeerConnection'].createDataChannel(
                    f"{peer_id}Channel",
                    # the application assumes that data channels are created manually on both peers
                    negotiated=True,
                    # data channels created with the same id are connected to each other across peers
                    id=0
                )

                # TODO
                # try:
                self.options["dataChannelHandler"](
                    self.our_peer_id, self.our_peer_id, self.peers, peer_id)
                # except:
                    # print(
                        # "Something with the data_channel_handler initialization went wrong")
            self.update_streams_logic(self.peers[peer_id])


            await self.update_negotiation_logic(self.peers[peer_id])

    def update_streams_logic(self, peer):
        """
        Updates the local and remote media streams according to the preferences set in options.

        Args:
            peer (dict): Data about the peer. Containes ["peerId"(string), "peerId" (string),"peerType" (string),"polite" (bool),"rtcPeerConnection" (RTCPeerConnection),"dataChannel" (RTCDataChannel),"makingOffer" (bool),"ignoreOffer" (bool),"isSettingRemoteAnswerPending" (bool)]
        """
        peerConnection = peer.get('rtcPeerConnection')

        # Get direction of transceivers
        direction = ""
        if self.options.get('enableLocalStream') and self.options.get('enableRemoteStream'):
            direction = "sendrecv"
        elif self.options.get('enableLocalStream'):
            direction = "sendonly"
        elif self.options.get('enableRemoteStream'):
            direction = "recvonly"
        else:
            direction = "inactive"

        # Add transceivers to peer connection
        if self.options.get('enableLocalStream') or self.options.get('enableRemoteStream'):
            #from aiortc.codecs import get_capabilities
            #caps = [c for c in get_capabilities('video').codecs if c.mimeType == 'video/VP8']
            #pc.addTransceiver('video', direction = 'recvonly').setCodecPreferences(caps)
            #peerConnection.addTransceiver('video', direction).setCodecPreferences(caps)
            peerConnection.addTransceiver('video', direction)
            #peerConnection.addTransceiver('audio', direction)

        # Activate streams and recorders based on preferences
        if self.options.get('enableLocalStream'):
            self.update_local_streams(peer)
            

        if self.options.get('enableRemoteStream'):
            #peer['remoteVideoStream'] = StreamViewer(peer['peerId']+".mp4")
            peer['remoteVideoStream'] = MediaBlackhole()
            self._logger.info(peer['remoteVideoStream'])
            @peerConnection.on('track')
            def on_track(track):
                peer['remoteVideoStream'].addTrack(track)

    def update_local_streams(self, peer):
        """
        Handles the local stream by creating a media source for camera devices and adds the to the peerConnection transceivers

        Args:
            peer (dict): Data about the peer. Containes ["peerId"(string), "peerId" (string),"peerType" (string),"polite" (bool),"rtcPeerConnection" (RTCPeerConnection),"dataChannel" (RTCDataChannel),"makingOffer" (bool),"ignoreOffer" (bool),"isSettingRemoteAnswerPending" (bool)]
        """
        peerConnection = peer.get('rtcPeerConnection')  
        options = {"video_size": "640x480",
            'rtbufsize': '160M',}
        if platform.system() == "Darwin":
            webcam = MediaPlayer(
            "default:none", format="avfoundation", options=options
        )
        elif platform.system() == "Windows":
            webcam = MediaPlayer(
            "video=Full HD webcam", format="dshow", options=options
        )
        #webcam = MediaPlayer("D:\\1.mp4")
        else:
            if self.options.get('cameraDevice'):
                if len(peer['media']) == 0:
                #webcam = MediaPlayer(self.options.get('cameraDevice'), format="v4l2", options=options)
                    webcam = MediaPlayer('rtsp://127.0.0.1:8554/ch0_0', format="rtsp", options=options)
                else:
                    webcam = MediaPlayer(self.filepath)
        peer['mediaplayer'] = webcam
        peer['localVideoStream'] = webcam.video
        audio = None
        
        for t in peerConnection.getTransceivers():
            self._logger.info(t)
            if t.kind == "audio" and webcam and webcam.audio:
                peerConnection.addTrack(webcam.audio)
            elif t.kind == "video" and webcam and webcam.video:
                peerConnection.addTrack(webcam.video)
                capabilities = RTCRtpSender.getCapabilities("video")
                preferences = list(filter(lambda x: x.name == "H264", capabilities.codecs))
                #preferences += list(filter(lambda x: x.name == "VP8", capabilities.codecs))
                preferences += list(filter(lambda x: x.name == "rtx", capabilities.codecs))
                #t = peerConnection.getTransceivers()[0]
                t.setCodecPreferences(preferences)


    async def update_negotiation_logic(self, peer):
        """
        Updates the negotiation logic of a new peer. Makes the RTCPeerConnection trigger and offer generation if the peers is impolite.

        Args:
            peer (dict): Data about the peer. Containes ["peerId"(string), "peerId" (string),"peerType" (string),"polite" (bool),"rtcPeerConnection" (RTCPeerConnection),"dataChannel" (RTCDataChannel),"makingOffer" (bool),"ignoreOffer" (bool),"isSettingRemoteAnswerPending" (bool)]
        """
        peerConnection = peer.get('rtcPeerConnection')
        
        try:
            if(not peer["polite"]):
                peer["makingOffer"] = True
                offer = await peerConnection.createOffer()
                await peerConnection.setLocalDescription(offer)
                if(self.verbose):
                    self._logger.info("Sending offer to {}".format(peer["peerId"]))
                data = {
                        "action": "ice_msg",
                        "sdpmessage": 
                                {
                                 "data": {"sdp": peerConnection.localDescription.sdp, 
                                            "type": peerConnection.localDescription.type},
                                 "type": "offer"
                                },
                        "to": peer['peerId']
                }
                self.websockets_client.send(json.dumps(data))
                peer["sdp"] = peerConnection.localDescription.sdp
                peer["type"] = peerConnection.localDescription.type
                sdpList = peerConnection.localDescription.sdp.split("\r\n")
                candidateList = []
                for i in sdpList:
                    if(i.find('candidate') > 0):
                        candidateList.append(i[2:])
                sdpMLineIndex = 0
                sdpMid = 0
                for i in candidateList:
                    if i != "end-of-candidates":                       
                        candidate = i
                        data = {
                            "action": "ice_msg",
                            "sdpmessage": 
                                    {
                                     "data": {'sdpMLineIndex': sdpMLineIndex, 
                                            'sdpMid': sdpMid,
                                            'candidate': candidate},
                                      "type":"candidate"
                                    },
                            "to": peer['peerId'],
                            }
                        self.websockets_client.send(json.dumps(data))
                    else:
                        sdpMLineIndex = 1
                        sdpMid = 1         
        except:
            self._logger.info('Something related to update_negotiation_logic went wrong')

        finally:
            peer["makingOffer"] = False

    async def update_session_description(self, peer, description, peerId):
        """
        The logic to update the session description protocol (SDP) during negotiations

        Args:
            peer (dict): Data about the peer. Containes ["peerId"(string), "peerId" (string),"peerType" (string),"polite" (bool),"rtcPeerConnection" (RTCPeerConnection),"dataChannel" (RTCDataChannel),"makingOffer" (bool),"ignoreOffer" (bool),"isSettingRemoteAnswerPending" (bool)]
            description (dict): Contains ["spd", "type"]
        """
        try:
            rtc_decription = RTCSessionDescription(
                description["sdp"], description["type"])
            peerConnection = peer['rtcPeerConnection']
            # if we recived and offer, check if there is an offer collision(ie. we already have created a local offer and tried to send it)
            offerCollision = description["type"] == "offer" and (
                peer["makingOffer"] or peerConnection.signalingState != "stable")
            peer["ignoreOffer"] = not peer["polite"] and offerCollision
            peer["peerId"] = peerId
            if(peer["ignoreOffer"]):
                if(self.verbose):
                    print("Peer offer was ignore because we are impolite")
                return

            if(offerCollision):
                # not working in wrtc node.js
                await peerConnection.setLocalDescription({"type": "rollback"})
                # not working in wrtc node.js
                await peerConnection.setRemoteDescription(rtc_decription)
            else:
                # Otherwise there are no collision and we can take the offer as our remote description
                await peerConnection.setRemoteDescription(rtc_decription)
                if(peer.get('remoteVideoStream')):
                    self._logger.info("### STARTING REMOTE STREAM this is the remote stream",peer.get('remoteVideoStream'))
                    await peer.get('remoteVideoStream').start()
            
            if(description["type"] == 'offer'):
                await peerConnection.setLocalDescription(await peerConnection.createAnswer())
                if(self.verbose):
                    self._logger.info("Sending answer to {}".format(peer["peerId"]))
                data = {
                        "action": "ice_msg",
                        "sdpmessage": 
                                {
                                 "data": {"sdp": peerConnection.localDescription.sdp, 
                                            "type": peerConnection.localDescription.type},
                                 "type": "answer"
                                },
                        "to": peer['peerId']
                }
                self.websockets_client.send(json.dumps(data))
                peer["sdp"] = peerConnection.localDescription.sdp
                peer["type"] = peerConnection.localDescription.type				
                sdpList = peerConnection.localDescription.sdp.split("\r\n")
                candidateList = []
                for i in sdpList:
                    if(i.find('candidate') > 0):
                        candidateList.append(i[2:])
                sdpMLineIndex = 0
                sdpMid = 0
                for i in candidateList:
                    if i != "end-of-candidates":                       
                        candidate = i
                        data = {
                            "action": "ice_msg",
                            "sdpmessage": 
                                    {
                                      "data": {'sdpMLineIndex': sdpMLineIndex, 
                                            'sdpMid': sdpMid,
                                            'candidate': candidate},
                                      "type":"candidate"
                                    },
                            "to": peer['peerId'],
                            }
                        self.websockets_client.send(json.dumps(data))
                    else:
                        sdpMLineIndex = 1
                        sdpMid = 1                  

        except:
            self._logger.info("Something related to update_session_description went wrong")

    async def update_ice_candidate(self, peer, candidate):
        """
        The logic to update the ICE Candidates during negotiation

        Args:
            peer (dict): Data about the peer. Containes ["peerId"(string), "peerId" (string),"peerType" (string),"polite" (bool),"rtcPeerConnection" (RTCPeerConnection),"dataChannel" (RTCDataChannel),"makingOffer" (bool),"ignoreOffer" (bool),"isSettingRemoteAnswerPending" (bool)]
            candidate (dict): The ICE Candidate from peer
        """
        peerConnection = peer["rtcPeerConnection"]
        tcpType = None
        raddr = None
        rport = None
        data = candidate["candidate"]
        if data == "":
            self._logger.info("candidate is void")
        else:
            sdpMLineIndex = int(candidate["sdpMLineIndex"])
            sdpMid = str(candidate["sdpMid"])
            data_list = data.split()
            foundation = data_list[0].replace('candidate:', '', 1)
            component = int(data_list[1])
            protocol = data_list[2]
            priority = int(data_list[3])
            address = data_list[4]
            port = int(data_list[5])
            type = data_list[7]       
            try:
                if data_list.index('tcptype') >= 0:
                    tcpType = data_list[data_list.index('tcptype') + 1]
            except:
                pass
            try:                   
                if data_list.index('raddr') >= 0:
                    raddr = data_list[data_list.index('raddr') + 1]
            except:
                pass
            try:             
                if data_list.index('rport') >= 0:
                    rport = int(data_list[data_list.index('rport') + 1])
            except:
                pass

            
            try:
                if(candidate != None):  # address == ip
                    rtc_candidate = RTCIceCandidate(component,
                                                    foundation,
                                                    address,
                                                    port,
                                                    priority,
                                                    protocol,
                                                    type,
                                                    raddr,
                                                    rport,
                                                    sdpMid = sdpMid,
                                                    sdpMLineIndex = sdpMLineIndex,
                                                    tcpType = tcpType)
                    await peerConnection.addIceCandidate(rtc_candidate)
            except:
                if(not peer["ignoreOffer"]):
                    self._logger.info("Something related to addIceCandidate went wrong")
            
    async def remove_peer(self, peerId):
        """
        Closes all connections and removes peer from connection. Fired when the peer has left the signaling server or when a close action is sent.

        Args:
            peer (dict): Data about the peer. Containes ["peerId"(string), "peerId" (string),"peerType" (string),"polite" (bool),"rtcPeerConnection" (RTCPeerConnection),"dataChannel" (RTCDataChannel),"makingOffer" (bool),"ignoreOffer" (bool),"isSettingRemoteAnswerPending" (bool)]
        """
        # # Close data channel
        # if(peer.get('dataChannel')):
        #     peer["dataChannel"].close()

        # # Close local streams
        # if(peer.get('localVideoStream')):
        #     await peer["localVideoStream"].stop()
        # if(peer.get('localAudioStream')):
        #     await peer["localAudioStream"].stop()

        # # Close remote streams
        # if(peer.get('remoteVideoStream')):
        #     await peer["remoteVideoStream"].stop()
        # if(peer.get('remoteAudioStream')):
        #     await peer["remoteAudioStream"].stop()

        # Close and remove connection
        #await peer["rtcPeerConnection"].close()
        peer_ids = list(self.peers.keys())
        for peer_id in peer_ids:                
            if peerId == peer_id:
                self.peers[peerId]["localVideoStream"].stop()
                await self.peers[peerId]["rtcPeerConnection"].close()
                del self.peers[peerId]
                if(self.verbose):
                    self._logger.info("Connection with {} has been removed".format(peerId))

    def getIceServers(self, deviceName):
        if self.region == 0:
            url = "https://api.crealitycloud.cn/api/cxy/v2/webrtc/iceServersJwt"
        else:
            url = "https://api.crealitycloud.com/api/cxy/v2/webrtc/iceServersJwt"
        data = '{"deviceName": "' + str(deviceName) + '" }'
        headers = {
            "Content-Type": "application/json",
            "__CXY_JWTOKEN_": self.token
        }
        response = requests.post(url, data=data, headers=headers, timeout=10).text
        res = json.loads(response)
        return res

    def track_states(self, peer):
        pc = peer.get('rtcPeerConnection')
        disconnect_id = peer.get('peerId')
        @pc.on("iceconnectionstatechange")
        def iceconnectionstatechange():
            timestamp = time.time()
            states = {
                        "iceState": pc.iceConnectionState,
                        "clientId": disconnect_id,
                        "connectedTime":int(timestamp),
            }
            peer["pushState"] = states
            self.push_state(self.websockets_client)
            if pc.iceConnectionState == "failed":
                #self.remove_peer(peer)
                peer_ids = list(self.peers.keys())
                for peer_id in peer_ids:                
                    if disconnect_id == peer_id:
                        self.close_queue.put(disconnect_id)

    # def track_states(self, pc, peer_id):
    #     # states = {
    #     #     "connectionState": [pc.connectionState],
    #     #     "iceConnectionState": [pc.iceConnectionState],
    #     #     "iceGatheringState": [pc.iceGatheringState],
    #     #     "signalingState": [pc.signalingState],
    #     # }

    #     # @pc.on("connectionstatechange")
    #     # def connectionstatechange():
    #     #     states["connectionState"].append(pc.connectionState)

    #     @pc.on("iceconnectionstatechange")
    #     def iceconnectionstatechange():
    #         states["iceConnectionState"].append(pc.iceConnectionState)
    #         self._timestamp = time.time()

    #     # @pc.on("icegatheringstatechange")
    #     # def icegatheringstatechange():
    #     #     states["iceGatheringState"].append(pc.iceGatheringState)

    #     # @pc.on("signalingstatechange")
    #     # def signalingstatechange():
    #     #     states["signalingState"].append(pc.signalingState)

    #     return states

    def push_state(self, ws):     
        peer_list = []
        peer_ids = list(self.peers.keys())
        for peer_id in peer_ids:
            pushState = self.peers[peer_id]["pushState"]
            peer_list.append(pushState)
            self._logger.info(str(pushState))
        if peer_list :
            self.count = 0
        else:
            self.count = self.count + 1
        self._logger.info("count down" + str(self.count))
        if self.count >= 10:
            self._logger.info("count to 10--" + "close all")
            self.close_queue.put("all")
            
        data = {
        "action": "push_state",
        "to":"server",
        "pushState": 
                    {
                        "pcPool": peer_list 
                    }
        }
        if ws is not None:
            ws.send(json.dumps(data))
    
    def token_update(self, token):
        self.token = str(token)
