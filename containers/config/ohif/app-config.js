// OHIF viewer config (spec §9). Single local DICOMweb source = Orthanc via the same-origin
// Caddy proxy (/dicom-web) — no CORS, no CDN (§9.4). Served under /viewer (routerBasename).
window.config = {
  // OHIF runs at the root of its own dedicated origin (the viewer port), so its baked-in
  // absolute /assets don't collide with the shell. DICOMweb is same-origin there (§9.4).
  routerBasename: "/",
  showStudyList: true,
  extensions: [],
  modes: [],
  defaultDataSourceName: "dicomweb",
  dataSources: [
    {
      friendlyName: "Orthanc (meld7t)",
      namespace: "@ohif/extension-default.dataSourcesModule.dicomweb",
      sourceName: "dicomweb",
      configuration: {
        name: "orthanc",
        wadoUriRoot: "/dicom-web",
        qidoRoot: "/dicom-web",
        wadoRoot: "/dicom-web",
        qidoSupportsIncludeField: true,
        supportsReject: false,
        imageRendering: "wadors",
        thumbnailRendering: "wadors",
        enableStudyLazyLoad: true,
        supportsFuzzyMatching: false,
        supportsWildcard: true,
        omitQuotationForMultipartRequest: true,
      },
    },
  ],
};
