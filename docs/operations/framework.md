# Framework Computer AMD 395+ Max

## Assembly

Follow the assembly instructions in https://guides.frame.work/Guide/Framework+Desktop+(AMD+Ryzen+AI+Max+300+Series)+DIY+Edition+Quick+Start+Guide/464 to install a 2TB+ disk, CPU fan.

## BIOS Settings

On first boot, enter the BIOS settings and change the following:

- Advanced
  - `iGPU Memory Configuration = Custom`
  - `iGPU Memory Size = 96GB`
- Security
  - enable the `Clear TPM` toggle
- Boot
  - enable `Power on AC Attach`
  - enable `Network Stack`
  - XXX HTTP??

Reboot, enter Settings again and check that the TPM state is `UnOwned`.
