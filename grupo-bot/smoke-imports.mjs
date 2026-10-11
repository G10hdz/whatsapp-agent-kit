// Smoke: verifica que la API de Baileys que usa wa-connect.mjs existe tras el bump a 7.x.
import assert from 'node:assert'
import makeWASocket, { useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } from 'baileys'

assert.equal(typeof makeWASocket, 'function')
assert.equal(typeof useMultiFileAuthState, 'function')
assert.equal(typeof fetchLatestBaileysVersion, 'function')
assert.equal(typeof DisconnectReason.loggedOut, 'number')
console.log('baileys API ok')
