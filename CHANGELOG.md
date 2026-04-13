# Changelog

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
