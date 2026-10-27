import assert from 'node:assert/strict'
import { test } from 'vitest'
import { verifyExeIdentityBytes, readPeResources } from './verify-exe-identity.mjs'

const align = value => (value + 3) & ~3
const product = 'LLANO Agent'
function versionBlock(key, value, children = [], type = 1) {
  const keyBytes = Buffer.from(`${key}\0`, 'utf16le'), start = align(6 + keyBytes.length)
  const length = children.reduce((sum, child) => align(sum) + child.length, start + value.length)
  const bytes = Buffer.alloc(length)
  bytes.writeUInt16LE(length, 0); bytes.writeUInt16LE(type === 1 ? value.length / 2 : value.length, 2); bytes.writeUInt16LE(type, 4)
  keyBytes.copy(bytes, 6); value.copy(bytes, start)
  let cursor = start + value.length
  for (const child of children) { cursor = align(cursor); child.copy(bytes, cursor); cursor += child.length }
  return bytes
}
function fixture({ wrongIcon = false, wrongProduct = false, wrongVersion = false, pe32 = false } = {}) {
  const images = [Buffer.from('first-brand-image'), Buffer.from('second-brand-image')]
  const ico = Buffer.alloc(6 + images.length * 16 + images.reduce((sum, image) => sum + image.length, 0))
  ico.writeUInt16LE(1, 2); ico.writeUInt16LE(images.length, 4)
  const group = Buffer.alloc(6 + images.length * 14)
  group.writeUInt16LE(1, 2); group.writeUInt16LE(images.length, 4)
  let at = 6 + images.length * 16
  for (let i = 0; i < images.length; i++) {
    const entry = 6 + i * 16, gentry = 6 + i * 14
    ico[entry] = ico[entry + 1] = 16 * (i + 1); ico.writeUInt16LE(1, entry + 4); ico.writeUInt16LE(32, entry + 6)
    ico.writeUInt32LE(images[i].length, entry + 8); ico.writeUInt32LE(at, entry + 12); images[i].copy(ico, at); at += images[i].length
    ico.copy(group, gentry, entry, entry + 12); group.writeUInt16LE(i + 1, gentry + 12)
  }
  const fixed = Buffer.alloc(52); fixed.writeUInt32LE(0xfeef04bd)
  for (const off of [8, 16]) { fixed.writeUInt32LE(18, off); fixed.writeUInt32LE((wrongVersion ? 7 : 6) * 65536, off + 4) }
  const text = (key, value) => versionBlock(key, Buffer.from(`${value}\0`, 'utf16le'))
  const version = versionBlock('VS_VERSION_INFO', fixed, [versionBlock('StringFileInfo', Buffer.alloc(0), [versionBlock('040904b0', Buffer.alloc(0), [
    text('ProductName', wrongProduct ? 'Electron' : product), text('FileDescription', product), text('FileVersion', '0.18.6.0'), text('ProductVersion', '0.18.6.0')
  ])])], 0)
  const resources = [
    { type: 3, id: 1, bytes: wrongIcon ? Buffer.from('wrong-brand-image') : images[0] },
    { type: 3, id: 2, bytes: images[1] }, { type: 14, id: 1, bytes: group }, { type: 16, id: 1, bytes: version }
  ]
  const rsrc = Buffer.alloc(8192); let cursor = 0
  const allocate = size => { const offset = cursor; cursor += size; return offset }
  const dir = entries => { const offset = allocate(16 + entries * 8); rsrc.writeUInt16LE(entries, offset + 14); return offset }
  const root = dir(3); assert.equal(root, 0)
  for (const [index, type] of [3, 14, 16].entries()) {
    const same = resources.filter(resource => resource.type === type), types = dir(same.length)
    rsrc.writeUInt32LE(type, 16 + index * 8); rsrc.writeUInt32LE((0x80000000 + types) >>> 0, 20 + index * 8)
    for (const [i, resource] of same.entries()) {
      const language = dir(1), data = allocate(16), payload = allocate(resource.bytes.length)
      rsrc.writeUInt32LE(resource.id, types + 16 + i * 8); rsrc.writeUInt32LE((0x80000000 + language) >>> 0, types + 20 + i * 8)
      rsrc.writeUInt32LE(1033, language + 16); rsrc.writeUInt32LE(data, language + 20)
      rsrc.writeUInt32LE(0x1000 + payload, data); rsrc.writeUInt32LE(resource.bytes.length, data + 4); resource.bytes.copy(rsrc, payload)
    }
  }
  const bytes = Buffer.alloc(512 + rsrc.length), pe = 0x80, optional = pe + 24, optionalSize = pe32 ? 224 : 240
  bytes.write('MZ'); bytes.writeUInt32LE(pe, 0x3c); bytes.write('PE\0\0', pe); bytes.writeUInt16LE(1, pe + 6); bytes.writeUInt16LE(optionalSize, pe + 20)
  bytes.writeUInt16LE(pe32 ? 0x10b : 0x20b, optional)
  const directory = optional + (pe32 ? 96 : 112) + 16
  bytes.writeUInt32LE(0x1000, directory); bytes.writeUInt32LE(rsrc.length, directory + 4)
  const section = optional + optionalSize
  bytes.writeUInt32LE(0x1000, section + 12); bytes.writeUInt32LE(rsrc.length, section + 16); bytes.writeUInt32LE(512, section + 20); rsrc.copy(bytes, 512)
  return { bytes, ico }
}

test.each([false, true])('verifies every brand image and release identity in PE32=%s', pe32 => {
  const { bytes, ico } = fixture({ pe32 })
  assert.deepEqual(verifyExeIdentityBytes(bytes, ico, product, '0.18.6'), { iconGroups: 1, iconImages: 2, versionResources: 1 })
})
test.each([
  [{ wrongIcon: true }, /icon image/], [{ wrongProduct: true }, /ProductName/], [{ wrongVersion: true }, /FileVersion/]
])('rejects incomplete or silently wrong resource edits: %j', (options, message) => {
  const { bytes, ico } = fixture(options)
  assert.throws(() => verifyExeIdentityBytes(bytes, ico, product, '0.18.6'), message)
})
test('rejects malformed PE offsets without reading outside the file', () => {
  const { bytes } = fixture(); bytes.writeUInt32LE(0xffffffff, 0x3c)
  assert.throws(() => readPeResources(bytes), /outside file/)
})
test('rejects an icon group referencing an unknown image ID', () => {
  const { bytes } = fixture()
  const resource = readPeResources(bytes).find(item => item.type === 14)
  // All fixture icon IDs are valid; making an ID unknown must not pass by checking strings alone.
  resource.bytes.writeUInt16LE(999, 18)
  assert.throws(() => verifyExeIdentityBytes(bytes, fixture().ico, product, '0.18.6'), /icon image/)
})
