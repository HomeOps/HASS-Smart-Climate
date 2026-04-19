# Changelog

## [1.0.0](https://github.com/HomeOps/HASS-Smart-Climate/compare/v0.2.1...v1.0.0) (2026-04-19)


### ⚠ BREAKING CHANGES

* Users whose wrapped climate device has a physical remote or knob will lose the automatic "drop out of preset when user overrides setpoint on the device" behavior. If that matters in your deployment, re-introduce the behavior explicitly (e.g. via an Automation on the real device''s state) rather than relying on implicit coupling.

### Code Refactoring

* make real climate a pure slave of smart climate ([#43](https://github.com/HomeOps/HASS-Smart-Climate/issues/43)) ([#44](https://github.com/HomeOps/HASS-Smart-Climate/issues/44)) ([e60d911](https://github.com/HomeOps/HASS-Smart-Climate/commit/e60d9114482a4ede34dc79e533ce2b4c3d0ea3cd))

## [0.2.1](https://github.com/HomeOps/HASS-Smart-Climate/compare/v0.2.0...v0.2.1) (2026-04-19)


### Bug Fixes

* keep preset when AUTO turns real device off ([#40](https://github.com/HomeOps/HASS-Smart-Climate/issues/40)) ([#41](https://github.com/HomeOps/HASS-Smart-Climate/issues/41)) ([8491a8a](https://github.com/HomeOps/HASS-Smart-Climate/commit/8491a8a5d2e5894880debfc4fbcee61879a375c6))

## [0.2.0](https://github.com/HomeOps/HASS-Smart-Climate/compare/v0.1.1...v0.2.0) (2026-04-18)


### Features

* turn off HVAC after 15 min stable in comfort band ([#38](https://github.com/HomeOps/HASS-Smart-Climate/issues/38)) ([dbab246](https://github.com/HomeOps/HASS-Smart-Climate/commit/dbab24607e94b767c907072fa851d7e8d4042a28))

## [0.1.1](https://github.com/HomeOps/HASS-Smart-Climate/compare/v0.1.0...v0.1.1) (2026-04-16)


### Bug Fixes

* use outside sensor to avoid excessive off/heat/cool cycling in comfort band ([#35](https://github.com/HomeOps/HASS-Smart-Climate/issues/35)) ([7aaa094](https://github.com/HomeOps/HASS-Smart-Climate/commit/7aaa0940b88bfd2a27bc5e04f37b2ab981eab871))

## [0.1.0](https://github.com/HomeOps/HASS-Smart-Climate/compare/v0.0.10...v0.1.0) (2026-04-14)


### Features

* turn off real climate when in-band; fix heat activation at low setpoint ([#32](https://github.com/HomeOps/HASS-Smart-Climate/issues/32)) ([e5d50dc](https://github.com/HomeOps/HASS-Smart-Climate/commit/e5d50dcf7a12ceba954e1229d774d9140e5a1099))

## [0.0.10](https://github.com/HomeOps/HASS-Smart-Climate/compare/v0.0.9...v0.0.10) (2026-04-13)


### Bug Fixes

* restore preset and sync real climate device on HA restart ([#29](https://github.com/HomeOps/HASS-Smart-Climate/issues/29)) ([a817bf4](https://github.com/HomeOps/HASS-Smart-Climate/commit/a817bf478772bc0b4caf24b33fd9761c39910970))

## [0.0.9](https://github.com/HomeOps/HASS-Smart-Climate/compare/v0.0.8...v0.0.9) (2026-04-13)


### Bug Fixes

* engage cooling at high - deadband instead of above high setpoint ([#26](https://github.com/HomeOps/HASS-Smart-Climate/issues/26)) ([e5dc4af](https://github.com/HomeOps/HASS-Smart-Climate/commit/e5dc4afd7ce15e2b072e09488724cf7ac2156400))

## [0.0.8](https://github.com/HomeOps/HASS-Smart-Climate/compare/v0.0.7...v0.0.8) (2026-04-09)


### Bug Fixes

* cool to `high - 1` to prevent integer-device upper-band overshoot ([#23](https://github.com/HomeOps/HASS-Smart-Climate/issues/23)) ([1bf6679](https://github.com/HomeOps/HASS-Smart-Climate/commit/1bf667936a58f506b62e322a87dd710f25a05296))

## [0.0.7](https://github.com/HomeOps/HASS-Smart-Climate/compare/v0.0.6...v0.0.7) (2026-04-08)


### Bug Fixes

* Return range midpoint as `temperature` in AUTO mode instead of null ([#16](https://github.com/HomeOps/HASS-Smart-Climate/issues/16)) ([38edd75](https://github.com/HomeOps/HASS-Smart-Climate/commit/38edd754454ee6a49395e7af8e0329ceea9e219e))

## [0.0.6](https://github.com/HomeOps/HASS-Smart-Climate/compare/v0.0.5...v0.0.6) (2026-04-08)


### Bug Fixes

* auto-publish releases on merge and auto-label PRs via release-drafter ([#18](https://github.com/HomeOps/HASS-Smart-Climate/issues/18)) ([f0b07c8](https://github.com/HomeOps/HASS-Smart-Climate/commit/f0b07c848188d74b03a8111f855d8fbf38d570f3))
* sync .release-please-manifest.json to 0.0.5 to match actual manifest.json version ([#20](https://github.com/HomeOps/HASS-Smart-Climate/issues/20)) ([f932244](https://github.com/HomeOps/HASS-Smart-Climate/commit/f9322441d7cf5f04b8fa66daedcaa68639d16815))
