# EXP-V1-0006 0.2.1 Contract Provenance

- Source repository: `AgentParadise/agent-paradise-standards-system`
- Source commit: `ee926c6e44c0e37ad524e0ee265ffc4fabb714fc` (merge of APSS#145)
- Source path: `standards-experimental/v1/EXP-V1-0006-workspace/`
- Experiment version: `0.2.1`
- Vendored here: `schemas/` (all eleven schema files, byte-for-byte)
- Vendored to `lib/python/agentic_isolation/tests/fixtures/apss_exp_v1_0006/`:
  `fixtures/` (valid, invalid, behaviour, launch-v1 and
  `file-probe.schema.json`, byte-for-byte)

The source commit must equal the `apss-workspace-standard` rev in the root
`Cargo.toml`; `tests/test_contract_provenance.py` enforces it and checks every
digest below. Regenerate only from a reviewed APSS revision, and update the
pin, this record and the files in one change.

## SHA-256

| File | SHA-256 |
|---|---|
| `agentic_isolation/contract/schemas/content-ref.schema.json` | `01209ab639f61521bcba186c18bf162169adb00fc9f891d425c2bdfb2fbfcc78` |
| `agentic_isolation/contract/schemas/credential-ref.schema.json` | `9d3fd7dd7667664bae9e404c8574d82515df825c2687929a8914be3d23e4bc45` |
| `agentic_isolation/contract/schemas/environment-ref.schema.json` | `997945ed7b759070facf241a1c1bba307c8f5e810bdd2e91f360e3a0002f1394` |
| `agentic_isolation/contract/schemas/workspace-finalization.schema.json` | `d5a540f7fd9bd490b785015d6c9783fa6fd3260cfa1208ba70b5c5bb2edf476d` |
| `agentic_isolation/contract/schemas/workspace-launch.schema.json` | `a16ad5dea0b4cb4119bc0fe967628a5eb906e7bc3bdef7d6c13cb45679863008` |
| `agentic_isolation/contract/schemas/workspace-observation.schema.json` | `102404a958f9644f7ccaf6488d12734ea146b12dfe8f0de39a1f2a424db5a6e5` |
| `agentic_isolation/contract/schemas/workspace-operation.schema.json` | `aabe6a41d532e49d83cb7bf4c041aeaade168d4e4bb34beac07ed2355064b70e` |
| `agentic_isolation/contract/schemas/workspace-provider.schema.json` | `0868a146761327a4fadfe47af61cd6c238aa06241fb84f8f227cf9b4ecb988a5` |
| `agentic_isolation/contract/schemas/workspace-ref.schema.json` | `76ff2bb6c266d1523dd1675a74f04f806f9b9154b87ab2c2807bc26fbceae960` |
| `agentic_isolation/contract/schemas/workspace-result.schema.json` | `d04cfb7004af6672f06cf6245beb3de4026ad3bdaea7dacd6d90000838ac9696` |
| `agentic_isolation/contract/schemas/workspace-status.schema.json` | `e941fbf144ca5bfcf36f60f58494333012ea20f45073e14cb580124e7a60312e` |
| `tests/fixtures/apss_exp_v1_0006/README.md` | `c17a3eb5a9298985bdb3b186eb5b6d6319f61a1a0711636a781e2ce776dea4f5` |
| `tests/fixtures/apss_exp_v1_0006/behaviour/cancel-twice-terminal-once.json` | `356a6ddf54f156877b75b971629204f5bc06f8879a79ecd680e2102abfa2ea8e` |
| `tests/fixtures/apss_exp_v1_0006/behaviour/cursor-after-retention-gap.json` | `bf6e99ed67356d9802e2b161469b23cff832949bede131702544012d1dc6f7e2` |
| `tests/fixtures/apss_exp_v1_0006/behaviour/hazard-cancel-before-launch-header.json` | `a0d30dafc208dc9759d0a581497aa447b242303fa53a9eace485cf73ede17396` |
| `tests/fixtures/apss_exp_v1_0006/behaviour/hazard-destroy-during-start.json` | `5514ab997f13c2cb62511e240c28fe2e003e5dae1a972df62586a3437dbba760` |
| `tests/fixtures/apss_exp_v1_0006/behaviour/hazard-double-fork-orphan.json` | `98301495567b74ab252ccbda0961a9d5463dc32579fd7bc20cab835d5c0855be` |
| `tests/fixtures/apss_exp_v1_0006/behaviour/hazard-drain-with-gap-reports-loss.json` | `cfa2e85adfcee7177c446a0eae43eae5260e6fe871acecb68fcd5833ba8d2999` |
| `tests/fixtures/apss_exp_v1_0006/behaviour/hazard-fifo-in-workspace.json` | `af23f22d9262e7d2c4e7935058269b772ea870f422738c05b2a11da0bfa05f8b` |
| `tests/fixtures/apss_exp_v1_0006/behaviour/hazard-interrupted-cancel.json` | `b99cb63694efe053437bab53aff7f91776e62aa701fc5a9729203670541cd387` |
| `tests/fixtures/apss_exp_v1_0006/behaviour/hazard-stuck-removal-timeout-settles.json` | `5ec373c5677e9bc312ebc43c99a99eeb86617e727f9f69dd536e48afaf54313a` |
| `tests/fixtures/apss_exp_v1_0006/behaviour/late-read-drains-then-complete.json` | `8fcf11e185ec909f984b8584ea0aa2301d11cc18918db080dde68e4cdb3db461` |
| `tests/fixtures/apss_exp_v1_0006/behaviour/required-capture-unknown-success-false.json` | `3bfd1eb12168dba3043a74cbec2f97472b7b0023d0a509b4563b66491fbf761c` |
| `tests/fixtures/apss_exp_v1_0006/behaviour/same-key-different-spec-conflict.json` | `e432cacb4e7022f833baeac0bd0965edc926af263037fa29ac077dc38b8117fd` |
| `tests/fixtures/apss_exp_v1_0006/behaviour/start-same-key-one-launch.json` | `b1320230a764bb33fcceb0789728e44e88bf31703dad95530cf3246029c33f55` |
| `tests/fixtures/apss_exp_v1_0006/behaviour/unknown-ref-typed-error.json` | `593decee879d4707f7908110958d1ae3cfd0400797c6dff832cd887fd6c68f9f` |
| `tests/fixtures/apss_exp_v1_0006/file-probe.schema.json` | `e25a565097c1179d16a154e7b67e10557e87de108f537d83aa8bf9534222b22f` |
| `tests/fixtures/apss_exp_v1_0006/invalid/complete-on-pending.json` | `c80fa675e7e64454c7d3eebfe0b47749448363c8f103f11b317a2105270b67b7` |
| `tests/fixtures/apss_exp_v1_0006/invalid/complete-on-running.json` | `bb32a71644c3a24cf4a28f8b0ff4fc55bfd67a797dd26b6bc1be41d119ede08b` |
| `tests/fixtures/apss_exp_v1_0006/invalid/content-ref-bad-digest.json` | `d79d675abab6b0a876235a8937dae5e4fb409a1cff996dbce9b64f28ec0f31aa` |
| `tests/fixtures/apss_exp_v1_0006/invalid/credential-binding-unknown-delivery.json` | `7d1112aa00d423f4735e22e9ecfab6998dfadeb9920d35516f4a8cb3497df7bf` |
| `tests/fixtures/apss_exp_v1_0006/invalid/credential-token-in-environment.json` | `9f11246f03dbebbabca9d733654b53672d127649d242062d31838cba3822ea0f` |
| `tests/fixtures/apss_exp_v1_0006/invalid/credential-value-in-environment.json` | `5e201d45e030f4b205589b4d7bd6020d3829511f22920785a5eb4b6a254a2a73` |
| `tests/fixtures/apss_exp_v1_0006/invalid/credential-value-in-operation-metadata.json` | `9da9a6bd29bd9c93d312dc5928d9135ae17b1551e588b602b9c7a389cc637920` |
| `tests/fixtures/apss_exp_v1_0006/invalid/credential-value-in-result-metadata.json` | `34b758be916c3c597ae4927cadb551b10f58aef6098147e54ada3546ce44a05c` |
| `tests/fixtures/apss_exp_v1_0006/invalid/credential-value-in-result-process-metadata.json` | `1b0f996c8ada26152df151abf794253247082d96a6d139b540eba5d031af4946` |
| `tests/fixtures/apss_exp_v1_0006/invalid/credential-value-in-status-metadata.json` | `b4534f00ddb121ba63cbc62cfa44918a5f62691aa0e51a66647c1dfccef3c6b2` |
| `tests/fixtures/apss_exp_v1_0006/invalid/credential-value-on-binding-ref.json` | `c0810f709e5793a00a4adc433cf6f841a0bc5e8a74f428ac09f982dfd1cdfc1f` |
| `tests/fixtures/apss_exp_v1_0006/invalid/credential-value-on-binding.json` | `416314ce3765e624a66d6b5314f562c56c128566cef36b0c5ff77cb9996af218` |
| `tests/fixtures/apss_exp_v1_0006/invalid/credential-value-on-content-authority.json` | `5e9b02b2c3d22056c15aa8c78fb993e44bd9aeb3ba009156f37c5eb0e9e224a8` |
| `tests/fixtures/apss_exp_v1_0006/invalid/credential-value-on-ref.json` | `7bba850d3214d63df514f7fff3901bb4a9540fd8f84cfa4cef4f2f1b0580ede0` |
| `tests/fixtures/apss_exp_v1_0006/invalid/descriptor-missing-enforceable-limits.json` | `3fd68ef331072278ae1405849258067e1c6f60289a02755851875705b81597fd` |
| `tests/fixtures/apss_exp_v1_0006/invalid/descriptor-partial-enforceable-limits.json` | `1670c9159b072af18ff78a0720569d6cfd4570348b2ffc06dfc2ab9123bb2312` |
| `tests/fixtures/apss_exp_v1_0006/invalid/error-kind-not-in-closed-set.json` | `9cedfe491227271086f2c9437e7f876817842f3ec4d0000ca4c098d7ef5d7dbb` |
| `tests/fixtures/apss_exp_v1_0006/invalid/finalization-completed-with-error.json` | `db9ee5af05aff0f4c1062aeecd2c505c3fa24a8ec39c7d598847065d3c798a59` |
| `tests/fixtures/apss_exp_v1_0006/invalid/finalization-retained-without-ref.json` | `db49133ce1ba6145fd2057caeafd07a9bb5c5395556a0a39a414f6180ee617c0` |
| `tests/fixtures/apss_exp_v1_0006/invalid/gap-lost-bytes-zero.json` | `dbcb658ed294f346500c45aaf50cd063444cbfc197b7ef1a6ce6623aedf1e1a9` |
| `tests/fixtures/apss_exp_v1_0006/invalid/gap-without-lost-bytes.json` | `380867fbd33027bef72cd2fff8bdab52fc35c738ae59ce891f0cad3a690bf7ad` |
| `tests/fixtures/apss_exp_v1_0006/invalid/operation-absolute-working-directory.json` | `434a889635ce095672911b21b0e989faeb03b9c5094e65ab92ef38e6b6c39e11` |
| `tests/fixtures/apss_exp_v1_0006/invalid/operation-empty-argv.json` | `22b596ed339099934a2944e40b7e6b5fdad6b999631fdf84d75a4f66c6e9f1c8` |
| `tests/fixtures/apss_exp_v1_0006/invalid/result-non-terminal-process.json` | `3e038c4b8a6e9d7d119be617916b7e4743e231e1a10ca1f9392c6b1a77600bed` |
| `tests/fixtures/apss_exp_v1_0006/invalid/running-with-exit-code.json` | `325d3b57dd1048d9d0104ecb9072d490208003241cc1b46bf8aeca3e302f8031` |
| `tests/fixtures/apss_exp_v1_0006/invalid/status-and-error-mixed.json` | `c61c3ae033fcfc9d64fb04ec3c53e65ce938d7389f3f4f3a0514d1906aa62d4d` |
| `tests/fixtures/apss_exp_v1_0006/invalid/succeeded-with-primary-failure.json` | `b15c0dea1a6fc03450e2068e50d24fbeaab4485e15f2f730f57b67925d75c9d6` |
| `tests/fixtures/apss_exp_v1_0006/invalid/success-false-when-it-succeeded.json` | `280124bea6c1dd82302848cde4c2cb35944ff0a33b0e6609f63ef228b222bc9f` |
| `tests/fixtures/apss_exp_v1_0006/invalid/success-true-with-failed-process.json` | `84a6fdb707c6a76bcb5fd4dd1e8c6012b4f9a5e9b7d1cbbd45bbcc5206b1b2f5` |
| `tests/fixtures/apss_exp_v1_0006/invalid/success-true-with-required-capture-unknown.json` | `e1bf3cf01a217b1450fe04bc65185a320de45d75185e9bf9203f69593c5f14fb` |
| `tests/fixtures/apss_exp_v1_0006/invalid/unknown-field-nested-ref.json` | `c436fdec25849c6c542ea4b14ba7be87c4b515c7591585d1ee1b653c4d13510e` |
| `tests/fixtures/apss_exp_v1_0006/invalid/unknown-field-operation.json` | `c3a0d74da14d44c724b2e860721fb28e70b985a1f19eba7f618ce4a168d3ddfd` |
| `tests/fixtures/apss_exp_v1_0006/invalid/wrong-discriminator.json` | `e273f7226413638700f40a93eed18253b1d0ac942477c99153b5e6502bb10748` |
| `tests/fixtures/apss_exp_v1_0006/launch-v1/invalid/absolute-working-directory.json` | `14f14cb97bf108347414311cc34ea85af7d4aba7e61f6c585c1d0896b8e9f43e` |
| `tests/fixtures/apss_exp_v1_0006/launch-v1/invalid/content-ref-input-is-v2-only.json` | `b1dff6f7da96fb5b02878ab119cf97a5d3fed04cee84740fb46fa0d82589503b` |
| `tests/fixtures/apss_exp_v1_0006/launch-v1/invalid/environment-field-is-v2-only.json` | `49988f68bf813cba8f15c5be323f07cfdf3fca247309587357cdc734b7497045` |
| `tests/fixtures/apss_exp_v1_0006/launch-v1/invalid/missing-transcript.json` | `f8fd244a2501448bf03b74d9ec5966c040e976db3b90d7bf96adc0b480ef2e32` |
| `tests/fixtures/apss_exp_v1_0006/launch-v1/invalid/unknown-top-level-field.json` | `b04c86413ab0ebd04177ece944065ef699417c0edb9f5bd3de6e6ab4bf0f577f` |
| `tests/fixtures/apss_exp_v1_0006/launch-v1/invalid/unpinned-skill-digest.json` | `1a6fca59d958b5bf94a21a01a7a0b69fc9d33468df3502c2a21797864a8414ca` |
| `tests/fixtures/apss_exp_v1_0006/launch-v1/invalid/wrong-schema-version.json` | `4c7a243d89083a0fcd17ac0fe66484c9846943036bfc541b041f3764b7e54cc9` |
| `tests/fixtures/apss_exp_v1_0006/launch-v1/invalid/zero-timeout.json` | `b6e23eae539f5d3017fc0556e813e3e197be1393915cf81f7739a051fc930ed4` |
| `tests/fixtures/apss_exp_v1_0006/launch-v1/valid/required-only.json` | `8bad096923dfc9bae21bc3ef1c4f8861fa48d0739209aefd769079d9361645a9` |
| `tests/fixtures/apss_exp_v1_0006/launch-v1/workspace-launch-0.1.0.schema.json` | `a16ad5dea0b4cb4119bc0fe967628a5eb906e7bc3bdef7d6c13cb45679863008` |
| `tests/fixtures/apss_exp_v1_0006/valid/content-ref/empty-content.json` | `84b8249fbbbcbd7fc6176c198cb2f2cf697b1b42fcf8392122898237f327db37` |
| `tests/fixtures/apss_exp_v1_0006/valid/content-ref/with-authority.json` | `50ac9d48859a88e537adb0d6ee33c12c7fd46da8b572c2527bd1b63df77560fe` |
| `tests/fixtures/apss_exp_v1_0006/valid/credential-ref/minimal.json` | `1350b33204f6389ce40046a716bae203257e7d9b6b80e951fdc73f96a032b51a` |
| `tests/fixtures/apss_exp_v1_0006/valid/environment-ref/oci-image.json` | `df065511a3b21ebbce51e339deee2d2cb18dd8ce76a7a6c22f92e7c15aaa11a2` |
| `tests/fixtures/apss_exp_v1_0006/valid/environment-ref/template.json` | `3e41e1538f3878a058493f24656227832a6f488b2b6e73dec5dc4e0767ad3e4a` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-finalization/completed.json` | `e03caab03113ab076ee63aa5050f2a59bbbaa137b41163b233268ef54148a557` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-finalization/failed-while-running.json` | `edfbc2fd24a3e1923f67227dc37f24e6d63e4141312e7e3291af821639cffea4` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-finalization/retained.json` | `bcd60a7dc03f5232d655dff10c8138cc2c641b3892539257db7ff98bff112a8f` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-observation/complete.json` | `ae9d407090582c66141a9825f035305a652597ac1587c259e0b433fec5b268b1` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-observation/gap-exact.json` | `cd72d5e93258192f6845f25c279ae563ff099a2b869011cfb5bd7f19bdb15f3b` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-observation/gap-unknown-extent.json` | `b528863fa6ad9bf4e0f2a0932db9954dee67402fc69154651b6ef8e733f17c13` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-observation/running.json` | `cac5a56253b53a16c0e84244ba13c5a84d1d2ffe9d87bb50641cd55605f95d3b` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-operation/full.json` | `b729ed7e5b42e73b820fd153f2fd1035ff32177a0d35617e67313266ccbe9a9e` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-operation/minimal.json` | `0cb4df5e05136bcde1d7a478efcf5360caaa74ea9e86f2625ca06a59ef3e01d0` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-provider/docker.json` | `49c5dfe08eee0b4d7523af3762767133461c71c0aefef779e4ae6f3aad893dc1` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-provider/local.json` | `43a2ff1422a33d36dfc3fccc9d5314ef65624a1948c02285b02ba76ce6edfe1d` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-provider/small-retention.json` | `edf4386fe7a482760803637c5b420d63bcc2a617e8a6ca1829d74641fff7d33d` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-ref/minimal.json` | `51a4339a5382f7a2e9faad450cd7a71bfd08967d7bac89e899d3b63b2ff3f260` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-result/capture-not-required.json` | `f4ed0f6b3703409ddd114a124a5e7f9aaad4efe626a062570ee0ed5748e3e7bc` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-result/capture-required-unknown.json` | `6d5b8f96d50ab677fe3945fb0e367a7e3dbab520d461af787cfc3bd91772d219` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-result/succeeded.json` | `19776f6e2566b583f3ef4648389b945151c7b66240fb1f9056b689cc4bdcaf66` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-result/timed-out-teardown-failed.json` | `348618f74b752a2af2548065b4f10631a28bad79330439a5bdcbc50e5caa3e35` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-status/error-provider-unavailable.json` | `ca997f3a2400fd3a7d61f1812aad27dc0d4d29ae43a0ffec8a64a1474906a243` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-status/error-unknown-operation.json` | `f61a2bee4bbefa0f980d0ba7e1360cc84b05f75a483fa8ca54f0641ac6eca916` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-status/running.json` | `4cd5c1946e0d5981c53e52e9e8702292bdd0a3ef9b7d2865a57468f8d1851e24` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-status/signalled.json` | `2da28114a5f239a1e2f682ee9c4a7aa8f290664be59f2d6ff2ceba97cf1719db` |
| `tests/fixtures/apss_exp_v1_0006/valid/workspace-status/succeeded.json` | `af442974f463ebd2dfe79a2f4dfac2df01b06a0b1b82be0b3dc22e215e5643eb` |
