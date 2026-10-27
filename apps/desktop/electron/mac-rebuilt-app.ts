import path from 'node:path'

function macRebuiltAppCandidates(updateRoot: string, productName: string) {
  const bundleName = `${productName}.app`

  return ['mac-arm64', 'mac'].map(outputDir =>
    path.join(updateRoot, 'apps', 'desktop', 'release', outputDir, bundleName)
  )
}

export { macRebuiltAppCandidates }
