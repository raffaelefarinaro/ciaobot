/**
 * The smallest RFC 6455 server that the PWA's two WebSocket clients need.
 *
 * Deliberately dependency-free: the e2e suite already costs every contributor
 * a Playwright install, and a real socket is the whole point of the reconnect
 * journey — Playwright's `routeWebSocket` mock would never exercise the
 * browser's own close/backoff path. The client only ever *receives* JSON here,
 * so the reader just has to recognise close frames and drop the rest.
 */
import crypto from 'node:crypto'

const GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

function encodeFrame(text) {
  const payload = Buffer.from(text, 'utf8')
  const len = payload.length
  let header
  if (len < 126) {
    header = Buffer.alloc(2)
    header[1] = len
  } else if (len < 65536) {
    header = Buffer.alloc(4)
    header[1] = 126
    header.writeUInt16BE(len, 2)
  } else {
    header = Buffer.alloc(10)
    header[1] = 127
    header.writeBigUInt64BE(BigInt(len), 2)
  }
  header[0] = 0x81 // FIN + text frame
  return Buffer.concat([header, payload])
}

/**
 * One accepted upgrade. `send` pushes a JSON frame; `drop` severs the TCP
 * connection without a close handshake, which is what a backgrounded iOS PWA
 * or a restarted host looks like from the browser's side.
 */
class FixtureSocket {
  constructor(socket, pathname) {
    this.socket = socket
    this.pathname = pathname
    this.closed = false
    /** The client's JSON frames, when the caller wants to read them. */
    this.onMessage = null
    this.buffer = Buffer.alloc(0)
    socket.on('close', () => { this.closed = true })
    socket.on('error', () => { this.closed = true })
    socket.on('data', (chunk) => this._read(chunk))
  }

  _read(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk])
    // Client frames are masked, and they can arrive split across chunks, so
    // they are decoded rather than guessed at.
    for (;;) {
      if (this.buffer.length < 2) return
      const opcode = this.buffer[0] & 0x0f
      const masked = (this.buffer[1] & 0x80) !== 0
      let length = this.buffer[1] & 0x7f
      let offset = 2
      if (length === 126) {
        if (this.buffer.length < 4) return
        length = this.buffer.readUInt16BE(2)
        offset = 4
      } else if (length === 127) {
        if (this.buffer.length < 10) return
        length = Number(this.buffer.readBigUInt64BE(2))
        offset = 10
      }
      let mask = null
      if (masked) {
        if (this.buffer.length < offset + 4) return
        mask = this.buffer.subarray(offset, offset + 4)
        offset += 4
      }
      if (this.buffer.length < offset + length) return
      const payload = Buffer.from(this.buffer.subarray(offset, offset + length))
      if (mask) {
        for (let i = 0; i < payload.length; i += 1) payload[i] ^= mask[i % 4]
      }
      this.buffer = this.buffer.subarray(offset + length)
      // Opcode 0x8 is a close frame; anything else from this client is noise
      // unless the caller asked to read it.
      if (opcode === 0x8) {
        this.drop()
        return
      }
      if (opcode === 0x1 && this.onMessage) {
        try { this.onMessage(payload.toString('utf8')) } catch { /* a bad frame is ignored */ }
      }
    }
  }

  send(message) {
    if (this.closed) return false
    try {
      this.socket.write(encodeFrame(JSON.stringify(message)))
      return true
    } catch {
      this.closed = true
      return false
    }
  }

  drop() {
    if (this.closed) return
    this.closed = true
    try { this.socket.destroy() } catch { /* already gone */ }
  }
}

/**
 * Accepts the upgrade and returns a FixtureSocket, or null when the request is
 * not a valid WebSocket handshake.
 */
export function acceptUpgrade(req, socket, pathname) {
  const key = req.headers['sec-websocket-key']
  if (!key) {
    socket.destroy()
    return null
  }
  const accept = crypto.createHash('sha1').update(key + GUID).digest('base64')
  socket.write(
    'HTTP/1.1 101 Switching Protocols\r\n'
    + 'Upgrade: websocket\r\n'
    + 'Connection: Upgrade\r\n'
    + `Sec-WebSocket-Accept: ${accept}\r\n\r\n`,
  )
  socket.setNoDelay(true)
  return new FixtureSocket(socket, pathname)
}
