import { readFileSync } from 'node:fs'
import path from 'node:path'
import { loadProductEnvironment } from './product-environment.mjs'
import { isMain } from './utils.mjs'

// Resource editing is not code signing. Inspect the PE bytes that will actually
// ship, instead of treating rcedit's successful exit as proof of branding.
export function readPeResources(bytes) {
  const range = (offset, size) => {
    if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(size) || offset < 0 || size < 0 || offset + size > bytes.length) {
      throw new Error('Invalid Windows executable: resource range outside file')
    }
    return bytes.subarray(offset, offset + size)
  }
  const u16 = offset => range(offset, 2).readUInt16LE()
  const u32 = offset => range(offset, 4).readUInt32LE()
  if (range(0, 2).toString() !== 'MZ') throw new Error('Invalid Windows executable: missing MZ header')
  const pe = u32(0x3c)
  if (!range(pe, 4).equals(Buffer.from([80, 69, 0, 0]))) throw new Error('Invalid Windows executable: missing PE header')
  const count = u16(pe + 6), optionalSize = u16(pe + 20), optional = pe + 24
  const magic = u16(optional)
  const directories = magic === 0x20b ? 112 : magic === 0x10b ? 96 : 0
  if (!directories || optionalSize < directories + 24) throw new Error('Invalid Windows executable: resource directory missing')
  const resourceRva = u32(optional + directories + 16), resourceSize = u32(optional + directories + 20)
  const sections = optional + optionalSize
  const mapRva = (rva, size) => {
    for (let i = 0; i < count; i++) {
      const section = sections + i * 40
      const address = u32(section + 12), rawSize = u32(section + 16), raw = u32(section + 20)
      if (rva >= address && rva - address + size <= rawSize) return range(raw + rva - address, size)
    }
    throw new Error('Invalid Windows executable: resource RVA outside section')
  }
  if (!resourceRva || !resourceSize) throw new Error('Windows executable has no resources')
  const directory = mapRva(resourceRva, resourceSize)
  const dRange = (offset, size) => {
    if (offset < 0 || offset + size > directory.length) throw new Error('Invalid Windows executable: resource directory overflow')
    return directory.subarray(offset, offset + size)
  }
  const resources = []
  let entryBudget = 65536
  const walk = (offset, ids) => {
    const header = dRange(offset, 16)
    const entries = header.readUInt16LE(12) + header.readUInt16LE(14)
    entryBudget -= entries
    if (entryBudget < 0) throw new Error('Invalid Windows executable: excessive resource entries')
    dRange(offset + 16, entries * 8)
    for (let i = 0; i < entries; i++) {
      const entry = dRange(offset + 16 + i * 8, 8), id = entry.readUInt32LE(0), child = entry.readUInt32LE(4)
      if (id & 0x80000000) {
        if ([3, 14, 16].includes(ids[0])) throw new Error('Unsupported named Windows branding resource')
        continue
      }
      const next = [...ids, id]
      if (child & 0x80000000) {
        if (next.length >= 3) throw new Error('Invalid Windows executable: excessive resource depth')
        walk(child & 0x7fffffff, next)
      } else {
        if (next.length !== 3) throw new Error('Invalid Windows executable: incomplete resource path')
        const data = dRange(child, 16)
        resources.push({ type: next[0], id: next[1], language: next[2], bytes: mapRva(data.readUInt32LE(0), data.readUInt32LE(4)) })
      }
    }
  }
  walk(0, [])
  return resources
}

function iconImages(ico) {
  if (ico.length < 6 || ico.readUInt32LE(0) !== 0x10000) throw new Error('Invalid brand ICO')
  const count = ico.readUInt16LE(4)
  if (!count || 6 + count * 16 > ico.length) throw new Error('Invalid brand ICO directory')
  return Array.from({ length: count }, (_, i) => {
    const at = 6 + i * 16, size = ico.readUInt32LE(at + 8), offset = ico.readUInt32LE(at + 12)
    if (offset < 6 + count * 16 || offset + size > ico.length) throw new Error('Invalid brand ICO image range')
    return { dimensions: ico.subarray(at, at + 8), bytes: ico.subarray(offset, offset + size) }
  })
}

function versionStrings(bytes) {
  const values = [], fixed = []
  const aligned = offset => (offset + 3) & ~3
  const block = (start, parentEnd, depth) => {
    if (depth > 8 || start + 6 > parentEnd) throw new Error('Invalid PE version resource')
    const length = bytes.readUInt16LE(start), valueLength = bytes.readUInt16LE(start + 2), type = bytes.readUInt16LE(start + 4)
    const end = start + length
    if (length < 6 || end > parentEnd) throw new Error('Invalid PE version resource length')
    let cursor = start + 6
    while (cursor + 2 <= end && bytes.readUInt16LE(cursor) !== 0) cursor += 2
    if (cursor + 2 > end) throw new Error('Invalid PE version resource key')
    const key = bytes.subarray(start + 6, cursor).toString('utf16le')
    const valueStart = aligned(cursor + 2), valueBytes = type === 1 ? valueLength * 2 : valueLength
    if (valueStart + valueBytes > end) throw new Error('Invalid PE version resource value')
    if (key === 'VS_VERSION_INFO' && type === 0 && valueBytes >= 52) {
      const value = bytes.subarray(valueStart, valueStart + valueBytes)
      if (value.readUInt32LE(0) !== 0xfeef04bd) throw new Error('Invalid PE fixed version signature')
      const parts = offset => {
        const ms = value.readUInt32LE(offset), ls = value.readUInt32LE(offset + 4)
        return [ms >>> 16, ms & 0xffff, ls >>> 16, ls & 0xffff].join('.')
      }
      fixed.push({ FileVersion: parts(8), ProductVersion: parts(16) })
    }
    if (type === 1 && valueLength) values.push([key, bytes.subarray(valueStart, valueStart + valueBytes).toString('utf16le').replace(/\0+$/, '')])
    cursor = aligned(valueStart + valueBytes)
    while (cursor + 6 <= end) cursor = aligned(block(cursor, end, depth + 1))
    return end
  }
  block(0, bytes.length, 0)
  return { values, fixed }
}

export function verifyExeIdentityBytes(executable, ico, productName, version) {
  const resources = readPeResources(executable), expected = iconImages(ico)
  const groups = resources.filter(resource => resource.type === 14)
  if (!groups.length) throw new Error('Windows executable is missing the brand icon')
  for (const group of groups) {
    const bytes = group.bytes
    if (bytes.length < 6 || bytes.readUInt32LE(0) !== 0x10000 || bytes.readUInt16LE(4) !== expected.length || bytes.length !== 6 + expected.length * 14) {
      throw new Error('Windows executable icon does not match brand ICO')
    }
    for (let i = 0; i < expected.length; i++) {
      const at = 6 + i * 14, imageId = bytes.readUInt16LE(at + 12)
      const image = resources.find(resource => resource.type === 3 && resource.id === imageId && resource.language === group.language)
      if (!image || !bytes.subarray(at, at + 8).equals(expected[i].dimensions) || bytes.readUInt32LE(at + 8) !== expected[i].bytes.length || !image.bytes.equals(expected[i].bytes)) {
        throw new Error('Windows executable icon image does not match brand ICO')
      }
    }
  }
  const versions = resources.filter(resource => resource.type === 16)
  if (!versions.length) throw new Error('Windows executable is missing product identity')
  for (const resource of versions) {
    const { values: strings, fixed } = versionStrings(resource.bytes)
    for (const field of ['ProductName', 'FileDescription']) {
      const matches = strings.filter(([key]) => key === field)
      if (!matches.length || matches.some(([, value]) => value !== productName)) throw new Error(`Windows executable ${field} does not match brand identity`)
    }
    if (version) {
      const normalize = value => {
        if (!/^\d+(?:\.\d+){0,3}$/.test(value)) throw new Error('Invalid Windows release version')
        return value.split('.').concat(['0', '0', '0']).slice(0, 4).map(Number).join('.')
      }
      const expectedVersion = normalize(version)
      for (const field of ['FileVersion', 'ProductVersion']) {
        const matches = strings.filter(([key]) => key === field)
        if (!fixed.length || fixed.some(value => value[field] !== expectedVersion) || !matches.length || matches.some(([, value]) => normalize(value) !== expectedVersion)) {
          throw new Error(`Windows executable ${field} does not match release version`)
        }
      }
    }
  }
  return { iconGroups: groups.length, iconImages: expected.length, versionResources: versions.length }
}

export function verifyExeIdentity(exe, icon, productName, version) {
  return verifyExeIdentityBytes(readFileSync(exe), readFileSync(icon), productName, version)
}

// Run on the EXE extracted from the final NSIS as well as the unpacked build:
// LLANO_APP_ENV=production node scripts/verify-exe-identity.mjs <extracted.exe>
if (isMain(import.meta.url)) {
  try {
    if (!process.argv[2]) throw new Error('Usage: verify-exe-identity.mjs <executable>')
    const root = path.resolve(import.meta.dirname, '..')
    const { version } = JSON.parse(readFileSync(path.join(root, 'package.json'), 'utf8'))
    const result = verifyExeIdentity(process.argv[2], path.join(root, 'assets', 'icon.ico'), loadProductEnvironment().productName, version)
    console.log(`[verify-exe-identity] verified ${result.iconImages} brand images and release ${version}`)
  } catch (error) {
    console.error(`[verify-exe-identity] ${error.message}`)
    process.exitCode = 1
  }
}
