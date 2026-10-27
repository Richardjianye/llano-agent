import { verifyMacDmg } from './verify-macos-release.mjs'

export default async function afterAllArtifactBuild(result, adapters = {}) {
  const verify = adapters.verify || verifyMacDmg
  const checksumPaths = []

  for (const artifact of result.artifactPaths.filter(filePath => filePath.endsWith('.dmg'))) {
    const verified = await verify(artifact)
    checksumPaths.push(verified.checksumPath)
  }

  return checksumPaths
}
