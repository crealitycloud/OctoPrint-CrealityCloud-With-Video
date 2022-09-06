import asyncio
import time
import websocket
import threading
import json
import logging

class WebSocketConnectionException(Exception):
    pass

class WebSocketClient:

    def __init__(self, url, queue, token ,subprotocols=None, waitsecs=120):
        self._logger = logging.getLogger("octoprint.plugins.crealitycloud")
        self._mutex = threading.RLock()
        self._name = "xiongrui"
        self._id = "105199"
        self._user_agent = "crealitycloud"
        self._url = url 
        self._queue = queue
        self.reconnect_count = 0
        self.subprotocols = subprotocols
        self.token = token

        self.ws = websocket.WebSocketApp(
            self._url,
            on_message=self.on_message,
            on_open=self.on_open,
            on_close=self.on_close,
            on_error=self.on_error,
            #header=header,
            subprotocols=self.subprotocols
        )
        wst = threading.Thread(target=self.ws.run_forever)
        wst.daemon = True
        wst.start()

        for i in range(waitsecs * 10):  # Give it up to 120s for ws hand-shaking to finish
            if self.connected():
                return
            time.sleep(0.1)
        self.ws.close()
        raise WebSocketConnectionException('Not connected to websocket server after {}s'.format(waitsecs))

    def send(self, data, as_binary=False):
        self._logger.info("send" + str(data))
        with self._mutex:
            if self.connected():
                if as_binary:
                    self.ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)
                    self._logger.info('send========================' + str(data))
                else:
                    self.ws.send(data)

    def connected(self):
        with self._mutex:
            return self.ws.sock and self.ws.sock.connected

    def close(self):
        with self._mutex:
            self.ws.keep_running = False
            self.ws.close()
			
    def on_error(self, ws, error):
        if type(error)==ConnectionRefusedError or type(error)==websocket._exceptions.WebSocketConnectionClosedException:
            self.close()
            self._logger.info("正在尝试第%d次重连"%self.reconnect_count)
            self.reconnect_count+=1
            if self.reconnect_count<100:
                self.connection_tmp(ws)
        else:
            self._logger.info("其他error!")

    def on_message(self, ws, msg):
        self._logger.info("recv++++++++++++" + str(msg))
        dict = json.loads(msg)
        action = dict["action"]
        if action != "join":
            self._queue.put(msg)


    def on_close(self, ws, status, msg):
         self._logger.info(str(status))
         self._logger.info(str(msg))

    def on_open(self, ws):
		# if on_ws_open:
		#     on_ws_open(ws)
        data = {                    
                        "action": "join",
                        "to": "server",
                        "clientCtx":{ 
                                        "device_brand":"raspberry",
                                        "os_version":"linux",
                                        "platform_type":1,
                                        "app_version":"v1.1.0"
                                    },
                        "token":{
                                    "jwtToken":self.token
                        }

				}
        ws.send(json.dumps(data))

    def connection_tmp(self, ws):
        #websocket.enableTrace(True)
        ws = websocket.WebSocketApp(            
            self._url,
            on_message=self.on_message,
            on_open=self.on_open,
            on_close=self.on_close,
            on_error=self.on_error,
            subprotocols=self.subprotocols)
        
        ws.on_open = self.on_open
        self.ws = ws
        try:
            wst = threading.Thread(target=ws.run_forever)
            wst.daemon = True
            wst.start() 
        except KeyboardInterrupt:
            ws.close()  
        except:
            ws.close() 

    def token_update(self, token):
        self.token = str(token)
