// OHIF viewer config for meld7t (spec §9). Modeled on the ohif/app image default, with the data
// source pointed at Orthanc over the same-origin /dicom-web proxy (no CORS, no CDN, §9.4).
// extensions/modes stay [] — the built-in set (longitudinal viewer + segmentation) is compiled
// into the bundle; emptying them (or removing them) breaks study opening.
window.config = {
  routerBasename: "/",
  extensions: [],
  modes: [],
  customizationService: {},
  showStudyList: true,
  maxNumberOfWebWorkers: 3,
  showCPUFallbackMessage: true,
  showLoadingIndicator: true,
  strictZSpacingForVolumeViewport: true,
  maxNumRequests: { interaction: 100, thumbnail: 75, prefetch: 25 },
  defaultDataSourceName: "dicomweb",
  dataSources: [
    {
      namespace: "@ohif/extension-default.dataSourcesModule.dicomweb",
      sourceName: "dicomweb",
      configuration: {
        friendlyName: "Orthanc (meld7t)",
        name: "orthanc",
        wadoUriRoot: "/dicom-web",
        qidoRoot: "/dicom-web",
        wadoRoot: "/dicom-web",
        qidoSupportsIncludeField: false,
        supportsReject: true,
        imageRendering: "wadors",
        thumbnailRendering: "wadors",
        enableStudyLazyLoad: true,
        supportsFuzzyMatching: false,
        supportsWildcard: true,
        omitQuotationForMultipartRequest: true,
        bulkDataURI: { enabled: true },
      },
    },
  ],
};
