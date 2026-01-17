# Framework Computer AMD 395+ Max

## Assembly

Follow the assembly instructions in https://guides.frame.work/Guide/Framework+Desktop+(AMD+Ryzen+AI+Max+300+Series)+DIY+Edition+Quick+Start+Guide/464 to install a 2TB+ disk, CPU fan.

## BIOS Settings

On first boot, press F2.  You should see different utilities, including Setup and Secure Boot.

### Setup Utility
- Advanced
  - `iGPU Memory Configuration = Custom`
  - `iGPU Memory Size = 96GB`
- Security
  - enable the `Clear TPM` toggle
  - disable `IO Interface -> Wifi and Bluetooth`
- Boot
  - enable `Power on AC Attach`
  - enable `Network Stack`
  - XXX HTTP??

### Secure Boot Utilities

- disable secure boot (TBD - reenable with Ubuntu)
- clear the secure boot info

