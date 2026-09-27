"""Temporary page-local focus for real input, never for background polling."""


def interactive(source):
    """Hidden Chrome menus close on blur. Focus only this owned target in CDP.

    This changes document focus emulation, not the Windows foreground window.
    Always restore the page on failure; never attach to other browser targets.
    """
    return r'''async page => {
 const session=await page.context().newCDPSession(page);
 let emulated=false;
 try {
   // Chrome can report hasFocus=true even while an OS-hidden page is invisible.
   // Menus need visibility emulation in that case too.
   if(!await page.evaluate(()=>document.hasFocus()&&!document.hidden)) {
     await session.send('Emulation.setFocusEmulationEnabled',{enabled:true});
     emulated=true;
   }
   return await (__ACTION__)(page);
 } finally {
   try {if(emulated)await session.send('Emulation.setFocusEmulationEnabled',{enabled:false});}
   finally {await session.detach();}
 }
}'''.replace('__ACTION__', source)
