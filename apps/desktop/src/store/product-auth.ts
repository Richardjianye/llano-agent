import { atom } from 'nanostores'

/**
 * Renderer-visible product authentication state.
 *
 * The login gate owns this value. Other full-screen recovery surfaces use it
 * only to avoid rendering backend failures underneath an intentional sign-out.
 */
export const $productAuthenticated = atom<boolean | null>(null)
