// wa-connect.mjs — bot de grupo. NÚMERO DESECHABLE.
// Objetivo Fase 1: "primer hola". Nada de Notion todavía.
// npm i baileys @hapi/boom qrcode-terminal  (declarado en package.json)
// Node 17+. Correr: WA_PHONE=52155... npm start
//
// OJO PROD: useMultiFileAuthState es solo para prototipo (IO-heavy).
// Para 24/7 migra las creds a Redis/DB. Para <20 miembros esto basta.

import makeWASocket, {
  useMultiFileAuthState,
  fetchLatestWaWebVersion,
  makeCacheableSignalKeyStore,
  DisconnectReason,
  Browsers,
} from 'baileys'
import { Boom } from '@hapi/boom'
import qrcode from 'qrcode-terminal'
import { rm } from 'node:fs/promises'

const USE_PAIRING_CODE = process.env.WA_PAIR_MODE !== 'qr'  // WA_PAIR_MODE=qr => QR en consola
const PHONE_NUMBER = process.env.WA_PHONE      // número DESECHABLE, solo dígitos con lada, sin + ( ) -
// Vacío => modo descubrimiento. Uno o varios JID separados por coma => manda "hola" a cada uno.
const GRUPO_JIDS = (process.env.GRUPO_JID || '').split(',').map(s => s.trim()).filter(Boolean)
let reconnectAttempts = 0
let holaEnviado = false                        // el "hola" se manda una sola vez por proceso

if (!PHONE_NUMBER) {
  console.error('❌ Falta WA_PHONE. Corre: WA_PHONE=52155XXXXXXXX npm start (número DESECHABLE, sin + ni espacios).')
  process.exit(1)
}

async function start() {
  // INVARIANTE DE SEGURIDAD: auth dir 'auth-grupo', separado del asistente personal (Proyecto B).
  const { state, saveCreds } = await useMultiFileAuthState('auth-grupo')
  const { version } = await fetchLatestWaWebVersion()  // clave contra cortes por versión vieja

  const sock = makeWASocket({
    version,
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys),
    },
    browser: Browsers.ubuntu('WAI-Bot'),
    markOnlineOnConnect: false,
    keepAliveIntervalMs: 30_000,
    connectTimeoutMs: 60_000,
    defaultQueryTimeoutMs: 60_000,
    printQRInTerminal: false,
    syncFullHistory: false,
  })

  // creds.me solo existe con sesión ya vinculada. Sin ese chequeo, un arranque normal sobre
  // una sesión buena pedía código nuevo y tumbaba el login (así se rompió la primera vez).
  if (USE_PAIRING_CODE && !sock.authState.creds.registered && !sock.authState.creds.me) {
    setTimeout(async () => {
      try {
        const code = await sock.requestPairingCode(PHONE_NUMBER)
        console.log('👉 Código de vinculación:', code, '(tienes ~60s para meterlo en el teléfono)')
      } catch (err) {
        // El socket pudo cerrarse antes de que corriera el timer. Sin este catch, el throw
        // queda sin capturar y tumba el proceso entero en vez de dejar que reconecte.
        console.log('⚠️  No se pudo pedir el código:', err?.output?.payload?.message || err?.message)
      }
    }, 3000)
  }

  sock.ev.on('creds.update', saveCreds)  // sin esto, falla la desencriptación

  sock.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update

    if (qr && !USE_PAIRING_CODE) qrcode.generate(qr, { small: true })

    if (connection === 'open') {
      reconnectAttempts = 0
      console.log('✅ Conectado como', sock.user?.id)

      if (GRUPO_JIDS.length === 0) {
        // MODO DESCUBRIMIENTO: lista los grupos con su JID para copiar el que quieras.
        const grupos = await listGroups(sock)
        console.log('\n=== Grupos (copia el JID que quieras usar) ===')
        for (const g of grupos) console.log(`  ${g.subject}  ->  ${g.id}`)
        console.log('\nLuego corre: GRUPO_JID=XXXX@g.us,YYYY@g.us npm start\n')
        return
      }

      if (!holaEnviado) {
        // MODO HOLA: un mensaje por grupo, una sola vez por proceso.
        holaEnviado = true
        for (const jid of GRUPO_JIDS) {
          await sock.sendMessage(jid, { text: 'hola 👋' })
          console.log('📨 "hola" enviado a', jid)
          // Pausa entre grupos: mandar en ráfaga desde un cliente no oficial dispara baneos.
          if (GRUPO_JIDS.length > 1) await new Promise(r => setTimeout(r, 3000))
        }
        console.log('— sigo vivo escuchando.')
      }
    }

    if (connection === 'close') {
      const code = (lastDisconnect?.error instanceof Boom)
        ? lastDisconnect.error.output?.statusCode
        : undefined

      // Única desconexión irrecuperable: logout real (401). Todo lo demás -> reconectar.
      if (code === DisconnectReason.loggedOut) {
        // Tras un 401 las creds ya no sirven: si se quedan, cada arranque reintenta login con
        // basura y vuelve a 401. Se borran para que el siguiente npm start vincule limpio.
        await rm('auth-grupo', { recursive: true, force: true })
        console.log('❌ Sesión cerrada (401). Borré ./auth-grupo. Corre npm start y mete el código en <60s.')
        process.exit(1)
      }
      // 515 (restartRequired) es NORMAL justo tras vincular: reconecta ya.
      const delay = Math.min(30_000, 1000 * 2 ** reconnectAttempts++)  // backoff exponencial
      console.log(`🔁 Cerró (code ${code}). Reintento en ${delay}ms`)
      setTimeout(start, delay)
    }
  })

  return sock
}

// ---- Helpers de administración de grupo ----
export async function sendToGroup(sock, groupJid, text) {
  return sock.sendMessage(groupJid, { text })
}
export async function listGroups(sock) {
  return Object.values(await sock.groupFetchAllParticipating())     // para sacar el @g.us de WAI
}

start()
