import { atom } from 'nanostores'

import { persistBoolean, persistString, storedBoolean, storedString } from '@/lib/storage'

const KEY = 'hermes.desktop.backdrop.v1'
const IMAGE_KEY = 'hermes.desktop.backdrop.image.v1'
const OPACITY_KEY = 'hermes.desktop.backdrop.opacity.v1'

/** Whether an image renders behind the chat transcript. */
export const $backdrop = atom(storedBoolean(KEY, true))

$backdrop.subscribe(on => persistBoolean(KEY, on))

export function setBackdrop(on: boolean) {
  $backdrop.set(on)
}

/**
 * User-chosen backdrop image, as a downscaled JPEG data URL — or null for the
 * built-in statue.
 *
 * Stored as a data URL in localStorage rather than a file path on purpose:
 * a path would need an Electron protocol handler to survive packaging (the
 * renderer can't reach arbitrary file:// URLs), and it would break the moment
 * the user moves or deletes the original. The picker downscales before
 * encoding (see prepareBackdropImage), so what lands here is bounded.
 */
export const $backdropImage = atom<null | string>(storedString(IMAGE_KEY))

$backdropImage.subscribe(value => persistString(IMAGE_KEY, value))

/** Opacity percent for a CUSTOM image. The built-in statue keeps its own wash. */
export const DEFAULT_BACKDROP_OPACITY = 15

export const $backdropOpacity = atom<number>(readOpacity())

$backdropOpacity.subscribe(value => persistString(OPACITY_KEY, String(value)))

function readOpacity(): number {
  const raw = Number(storedString(OPACITY_KEY))

  // NaN (never set / corrupt) falls back to the default rather than to 0 —
  // a 0% backdrop is indistinguishable from "the feature is broken".
  return Number.isFinite(raw) ? clampOpacity(raw) : DEFAULT_BACKDROP_OPACITY
}

export function clampOpacity(value: number): number {
  return Math.min(100, Math.max(0, Math.round(value)))
}

export function setBackdropOpacity(percent: number) {
  $backdropOpacity.set(clampOpacity(percent))
}

export function setBackdropImage(dataUrl: null | string) {
  $backdropImage.set(dataUrl)
}

/** Longest edge, in px, we keep when re-encoding a picked image. */
export const BACKDROP_MAX_EDGE = 1600

/** Refuse anything that still encodes larger than this — localStorage is ~5MB. */
export const BACKDROP_MAX_BYTES = 2_000_000

export class BackdropImageTooLarge extends Error {}

/**
 * Decode *file*, downscale it to at most BACKDROP_MAX_EDGE on its longest side,
 * and return a JPEG data URL.
 *
 * Re-encoding is the whole point: a 12MP phone photo is ~6MB of PNG/HEIC and
 * would blow the localStorage quota, and nothing behind a 2.5%-opacity wash
 * needs that resolution.
 */
export async function prepareBackdropImage(file: Blob): Promise<string> {
  const bitmap = await createImageBitmap(file)

  try {
    const scale = Math.min(1, BACKDROP_MAX_EDGE / Math.max(bitmap.width, bitmap.height))
    const width = Math.max(1, Math.round(bitmap.width * scale))
    const height = Math.max(1, Math.round(bitmap.height * scale))
    const canvas = document.createElement('canvas')

    canvas.width = width
    canvas.height = height

    const context = canvas.getContext('2d')

    if (!context) {
      throw new Error('canvas 2d context unavailable')
    }

    context.drawImage(bitmap, 0, 0, width, height)

    const dataUrl = canvas.toDataURL('image/jpeg', 0.72)

    if (dataUrl.length > BACKDROP_MAX_BYTES) {
      throw new BackdropImageTooLarge('encoded backdrop exceeds the storage budget')
    }

    return dataUrl
  } finally {
    bitmap.close()
  }
}
