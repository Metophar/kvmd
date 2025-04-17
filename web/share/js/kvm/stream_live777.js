/*****************************************************************************
#                                                                            #
#    KVMD - The main PiKVM daemon.                                           #
#                                                                            #
#    Copyright (C) 2018-2024  Maxim Devaev <mdevaev@gmail.com>               #
#                                                                            #
#    This program is free software: you can redistribute it and/or modify    #
#    it under the terms of the GNU General Public License as published by    #
#    the Free Software Foundation, either version 3 of the License, or       #
#    (at your option) any later version.                                     #
#                                                                            #
#    This program is distributed in the hope that it will be useful,         #
#    but WITHOUT ANY WARRANTY; without even the implied warranty of          #
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the           #
#    GNU General Public License for more details.                            #
#                                                                            #
#    You should have received a copy of the GNU General Public License       #
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.  #
#                                                                            #
*****************************************************************************/


"use strict";


import {tools, $} from "../tools.js";


var _Live777 = null;


export function Live777Streamer(__setActive, __setInactive, __setInfo, __orient, __allow_audio, __allow_mic) {
    var self = this;

    /************************************************************************/

    __allow_mic = (__allow_audio && __allow_mic); // Mic only with audio

    var __stop = false;
    var __ensuring = false;

    var __live777 = null;
    var __handle = null;

    var __retry_ensure_timeout = null;
    var __retry_emsg_timeout = null;
    var __info_interval = null;

    var __state = null;
    var __frames = 0;

    var __ice = null;

    /************************************************************************/

    self.getOrientation = () => __orient;
    self.isAudioAllowed = () => __allow_audio;
    self.isMicAllowed = () => __allow_mic;

    self.getName = function() {
        let name = "Live777 H.264";
        if (__allow_audio) {
            name += " + Audio";
            if (__allow_mic) {
                name += " + Mic";
            }
        }
        return name;
    };
    self.getMode = () => "live777";

    self.getResolution = function() {
        let el = $("stream-video");
        return {
            "real_width": (el.videoWidth || el.offsetWidth),
            "real_height": (el.videoHeight || el.offsetHeight),
            "view_width": el.offsetWidth,
            "view_height": el.offsetHeight,
        };
    };

    self.ensureStream = function(state) {
        __state = state;
        __stop = false;
        __ensureLive777(false);
    };

    self.stopStream = function() {
        __stop = true;
        __destroyLive777();
    };

    var __ensureLive777 = function(internal) {
        if (__live777 === null && !__stop && (!__ensuring || internal)) {
            __ensuring = true;
            __setInactive();
            __setInfo(false, false, "");
            __logInfo("Starting Live777 ...");
            __live777 = new _Live777({
                "server": tools.makeWsUrl("live777/ws"),
                "ipv6": true,
                "destroyOnUnload": false,
                "iceServers": () => __getIceServers(),
                "success": __attachLive777,
                "error": function(error) {
                    __logError(error);
                    __setInfo(false, false, error);
                    __finishLive777();
                },
            });
        }
    };

    var __getIceServers = function() {
        if (__ice !== null && __ice.url) {
            __logInfo("Using the custom ICE Server:", __ice);
            return [{"urls": __ice.url}];
        } else {
            return [];
        }
    };

    var __finishLive777 = function() {
        if (__stop) {
            if (__retry_ensure_timeout !== null) {
                clearTimeout(__retry_ensure_timeout);
                __retry_ensure_timeout = null;
            }
            __ensuring = false;
        } else {
            if (__retry_ensure_timeout === null) {
                __retry_ensure_timeout = setTimeout(function() {
                    __retry_ensure_timeout = null;
                    __ensureLive777(true);
                }, 5000);
            }
        }
        __stopRetryEmsgInterval();
        __stopInfoInterval();
        if (__handle) {
            __logInfo("Live777 detaching ...");
            __handle.detach();
            __handle = null;
        }
        __live777 = null;
        __setInactive();
        if (__stop) {
            __setInfo(false, false, "");
        }
    };

    var __destroyLive777 = function() {
        if (__live777 !== null) {
            __live777.destroy();
        }
        __finishLive777();
        let stream = $("stream-video").srcObject;
        if (stream) {
            for (let track of stream.getTracks()) {
                __removeTrack(track);
            }
        }
    };

    var __addTrack = function(track) {
        let el = $("stream-video");
        if (el.srcObject) {
            for (let tr of el.srcObject.getTracks()) {
                if (tr.kind === track.kind && tr.id !== track.id) {
                    __removeTrack(tr);
                }
            }
        }
        if (!el.srcObject) {
            el.srcObject = new MediaStream();
        }
        el.srcObject.addTrack(track);
    };

    var __removeTrack = function(track) {
        let el = $("stream-video");
        if (!el.srcObject) {
            return;
        }
        track.stop();
        el.srcObject.removeTrack(track);
        if (el.srcObject.getTracks().length === 0) {
            el.srcObject = null;
        }
    };

    var __attachLive777 = function() {
        if (__live777 === null) {
            return;
        }

        __handle = __live777.createHandle({
            "plugin": "live777.plugin.ustreamer",
            "success": function(handle) {
                __logInfo("Live777 attached:", handle.getId());
                __logInfo("Sending FEATURES ...");
                handle.sendMessage({"request": "features"});
            },
            "error": function(error) {
                __logError("Can't attach Live777:", error);
                __setInfo(false, false, error);
                __destroyLive777();
            },
            "onmessage": function(msg, jsep) {
                __stopRetryEmsgInterval();

                if (msg.result) {
                    __logInfo("Got Live777 result message:", msg.result);
                    if (msg.result.status === "started") {
                        __setActive();
                        __setInfo(false, false, "");
                    } else if (msg.result.status === "stopped") {
                        __setInactive();
                        __setInfo(false, false, "");
                    } else if (msg.result.status === "features") {
                        tools.feature.setEnabled($("stream-audio"), msg.result.features.audio);
                        tools.feature.setEnabled($("stream-mic"), msg.result.features.mic);
                        __ice = msg.result.features.ice;
                        __sendWatch();
                    }
                } else if (msg.error) {
                    __logError("Got Live777 error message:", msg.error);
                    __setInfo(false, false, msg.error);
                    if (__retry_emsg_timeout === null) {
                        __retry_emsg_timeout = setTimeout(function() {
                            if (!__stop) {
                                __sendStop();
                                __sendWatch();
                            }
                            __retry_emsg_timeout = null;
                        }, 2000);
                    }
                    return;
                }

                if (jsep) {
                    __logInfo("Handling SDP:", jsep);
                    __handle.createAnswer({
                        "jsep": jsep,
                        "tracks": [
                            {"type": "video", "capture": false, "recv": true},
                            {"type": "audio", "capture": __allow_mic, "recv": __allow_audio}
                        ],
                        "success": function(jsep) {
                            __logInfo("Got SDP:", jsep);
                            __sendStart(jsep);
                        },
                        "error": function(error) {
                            __logError("Error creating answer:", error);
                            __setInfo(false, false, error);
                        }
                    });
                }
            },
            "ontrack": function(track, on) {
                __logInfo("Got track:", track.kind, on);
                if (on) {
                    __addTrack(track);
                    if (track.kind === "video") {
                        __startInfoInterval();
                    }
                } else {
                    __removeTrack(track);
                }
            },
            "onclose": function() {
                __logInfo("Connection closed");
                __stopInfoInterval();
            }
        });
    };

    var __startInfoInterval = function() {
        __stopInfoInterval();
        __setActive();
        __updateInfo();
        __info_interval = setInterval(__updateInfo, 1000);
    };

    var __stopInfoInterval = function() {
        if (__info_interval !== null) {
            clearInterval(__info_interval);
            __info_interval = null;
        }
    };

    var __stopRetryEmsgInterval = function() {
        if (__retry_emsg_timeout !== null) {
            clearTimeout(__retry_emsg_timeout);
            __retry_emsg_timeout = null;
        }
    };

    var __updateInfo = function() {
        if (__handle !== null) {
            let info = "";
            let stats = __handle.getStats();
            if (stats) {
                info = `${stats.bitrate} kbps`;
                let frames = stats.fps;
                if (frames !== null) {
                    info += ` / ${frames} fps`;
                }
            }
            __setInfo(true, __isOnline(), info);
        }
    };

    var __isOnline = function() {
        return !!(__state && __state.source.online);
    };

    var __sendWatch = function() {
        if (__handle) {
            __logInfo(`Sending WATCH(orient=${__orient}, audio=${__allow_audio}, mic=${__allow_mic}) ...`);
            __handle.sendMessage({
                "request": "watch",
                "orientation": __orient,
                "audio": __allow_audio,
                "mic": __allow_mic
            });
        }
    };

    var __sendStart = function(jsep) {
        if (__handle) {
            __logInfo("Sending START ...");
            __handle.sendMessage({"request": "start"}, jsep);
        }
    };

    var __sendStop = function() {
        __stopInfoInterval();
        if (__handle) {
            __logInfo("Sending STOP ...");
            __handle.sendMessage({"request": "stop"});
            __handle.hangup();
        }
    };

    var __logInfo = (...args) => tools.info("Stream [Live777]:", ...args);
    var __logError = (...args) => tools.error("Stream [Live777]:", ...args);
}

Live777Streamer.ensure_live777 = function(callback) {
    if (_Live777 === null) {
        import("./live777.js").then((module) => {
            module.Live777.init({
                "debug": "all",
                "callback": function() {
                    _Live777 = module.Live777;
                    callback(true);
                },
            });
        }).catch((ex) => {
            tools.error("Stream: Can't import Live777 module:", ex);
            callback(false);
        });
    } else {
        callback(true);
    }
};

Live777Streamer.is_webrtc_available = function() {
    return !!window.RTCPeerConnection;
}; 