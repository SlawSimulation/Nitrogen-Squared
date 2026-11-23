// SPDX-License-Identifier: GPL-3.0-or-later
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
if (import.meta.env.DEV && !window.__TAURI_INTERNALS__) await import('./dev/mock.js');
export const native = (cmd,args) => invoke(cmd,args);
export const on = (event,fn) => listen(event,e=>fn(e.payload));
const pickers={choose:'choose_disc',choose_mod:'choose_mod'};
export const request = (command,args={}) => pickers[command] ? invoke(pickers[command]) : invoke('request',{command,args});
