from . import utils
from . import gui
from electroncash.i18n import _, language
from electroncash.mnemonic import Mnemonic
from typing import Any
from .uikit_bindings import *
from .custom_objc import *
import sys

def PresentAddWalletWizard(vc : ObjCInstance = None, animated : bool = True, completion : Block = None, dontPresentJustReturnIt = False) -> ObjCInstance:
    if not vc: vc = gui.ElectrumGui.gui.get_presented_viewcontroller()
    sb = UIStoryboard.storyboardWithName_bundle_("NewWallet", None)
    if not sb:
        utils.NSLog("ERROR: SB IS NULL")
        return None
    nav = sb.instantiateViewControllerWithIdentifier_("Add_A_Wallet")
    #nav = sb.instantiateViewControllerWithIdentifier_("TESTIE")
    if nav:
        if not dontPresentJustReturnIt:
            vc.presentViewController_animated_completion_(nav, animated, completion)
    else:
        utils.NSLog("ERROR: Could not find the storyboard viewcontroller named 'Add_A_Wallet'!")
    return nav


class NewWalletVC(NewWalletVCBase):
    origPlaceholders = objc_property()
    origLabelTxts = objc_property()

    @objc_method
    def dealloc(self) -> None:
        # cleanup code here
        self.origPlaceholders = None
        self.origLabelTxts = None
        send_super(__class__, self, 'dealloc')


    @objc_method
    def viewDidLoad(self) -> None:
        send_super(__class__, self, 'viewDidLoad')
        
    @objc_method
    def translateUI(self) -> None:
        lbls = [ self.walletNameTit, self.walletPw1Tit, self.walletPw2Tit ]
        if not self.origLabelTxts:
            self.origLabelTxts = { lbl.ptr.value : lbl.text for lbl in lbls }
        d = self.origLabelTxts
        for lbl in lbls:
            lbl.setText_withKerning_(_(d[lbl.ptr.value]), utils._kern)
                
        tfs = [ self.walletName, self.walletPw1, self.walletPw2 ]
        if not self.origPlaceholders:
            self.origPlaceholders = { tf.ptr.value : tf.placeholder for tf in tfs }
        d = self.origPlaceholders
        for tf in tfs:
            tf.placeholder = _(d[tf.ptr.value])
            utils.uitf_redo_attrs(tf)
    
    @objc_method
    def viewWillAppear_(self, animated : bool) -> None:
        send_super(__class__, self, 'viewWillAppear:', animated, argtypes=[c_bool])
        self.translateUI()

    @objc_method
    def viewWillDisappear_(self, animated : bool) -> None:
        send_super(__class__, self, 'viewWillDisappear:', animated, argtypes=[c_bool])
        self.view.endEditing_(True)

    
    @objc_method
    def textFieldShouldReturn_(self, tf) -> bool:
        tf.resignFirstResponder()
        return True
    
    @objc_method
    def textFieldDidEndEditing_(self, tf : ObjCInstance) -> None:
        if tf.ptr == self.walletName.ptr:
            tf.text = utils.pathsafeify(tf.text)[:30]
        utils.uitf_redo_attrs(tf)

    @objc_method
    def textFieldDidBeginEditing_(self, tf : ObjCInstance) -> None:
        pass
    
    
    @objc_method
    def doChkFormOk(self) -> bool:
        self.walletName.text = utils.pathsafeify(self.walletName.text)
        errMsg = ''
        if not self.walletName.text:
            errMsg = _("Wallet name is empty. Please enter a wallet name to proceed.")
        elif gui.ElectrumGui.gui.check_wallet_exists(self.walletName.text):
            errMsg = _("A wallet with that name already exist. Please enter a different wallet name to proceed.")
        elif not self.walletPw1.text:
            errMsg = _("Wallet password is empty. Please set a wallet password to proceed. You can disable wallet password protection later if you wish.")
        elif self.walletPw1.text != self.walletPw2.text:
            errMsg = _("Wallet passwords do not match. Please confirm the password you wish to set for your wallet by entering the same password twice.")

        if errMsg:
            utils.uilabel_replace_attributed_text(self.errMsg, errMsg, font = UIFont.italicSystemFontOfSize_(14.0))
        self.errMsgView.setHidden_(not errMsg)
        return not errMsg

    @objc_method
    def shouldPerformSegueWithIdentifier_sender_(self, identifier, sender) -> bool:
        # check passwords match, wallet name is unique
        return self.doChkFormOk()
        
    
    @objc_method
    def prepareForSegue_sender_(self, segue, sender) -> None:
        # pass along wallet name, password, etc..
        _SetParam(self, 'WalletName', self.walletName.text)
        _SetParam(self, 'WalletPass', self.walletPw2.text)

class NewWalletSeed1(NewWalletSeedBase):
    origLabelTxts = objc_property()
    seed = objc_property()

    @objc_method
    def dealloc(self) -> None:
        # cleanup code here
        self.origLabelTxts = None
        self.seed = None
        send_super(__class__, self, 'dealloc')

    @objc_method
    def viewDidLoad(self) -> None:
        send_super(__class__, self, 'viewDidLoad')
        utils.uilabel_replace_attributed_text(self.seedtv, " ", font = UIFont.systemFontOfSize_weight_(16.0, UIFontWeightBold))
        
    @objc_method
    def translateUI(self) -> None:
        lbls = [ self.seedTit, self.info ]
        if not self.origLabelTxts:
            self.origLabelTxts = { lbl.ptr.value : lbl.text for lbl in lbls }
        d = self.origLabelTxts
        for lbl in lbls:
            if lbl.ptr == self.info.ptr:
                utils.uilabel_replace_attributed_text(lbl, _(d[lbl.ptr.value]), font=UIFont.italicSystemFontOfSize_(14.0))
            else:
                lbl.setText_withKerning_(_(d[lbl.ptr.value]), utils._kern)
   
    @objc_method
    def viewWillAppear_(self, animated : bool) -> None:
        send_super(__class__, self, 'viewWillAppear:', animated, argtypes=[c_bool])
        self.translateUI()
        self.infoView.setHidden_(True)

    @objc_method
    def viewDidAppear_(self, animated : bool) -> None:
        send_super(__class__, self, 'viewDidAppear:', animated, argtypes=[c_bool])
        if not self.seed:
            self.infoView.setHidden_(True)
            def GenSeed() -> str:
                return _Mnem().make_seed()
            def OnError(exc) -> None:
                def onOk() -> None:
                    self.presentingViewController.dismissViewControllerAnimated_completion_(True, None)
                gui.ElectrumGui.gui.show_error(str(exc[1]), onOk = onOk, vc = self)
            def OnSuccess(result : str) -> None:
                self.infoView.setHidden_(False)
                self.seed = result
                utils.uilabel_replace_attributed_text(self.seedtv, self.seed)        
            utils.WaitingDialog(self, _("Generating seed..."), GenSeed,  OnSuccess, OnError)
        else:
            self.infoView.setHidden_(False)
            utils.uilabel_replace_attributed_text(self.seedtv, self.seed)
    
    @objc_method
    def shouldPerformSegueWithIdentifier_sender_(self, identifier, sender) -> bool:
        return bool(self.seed)
        
    
    @objc_method
    def prepareForSegue_sender_(self, segue, sender) -> None:
        # pass along wallet seed
        s = py_from_ns(self.seed)
        sl = py_from_ns(self.seed).split()
        _SetParam(self, 'seed', s)
        _SetParam(self, 'seed_list', sl)
        if isinstance(segue.destinationViewController, NewWalletSeed2):
            segue.destinationViewController.seed = s
            segue.destinationViewController.seedList = sl
        #print("params=",_Params(self))

class NewWalletSeed2(NewWalletSeedBase):
    origLabelTxts = objc_property()
    seed = objc_property()
    seedList = objc_property()
    sugButs = objc_property()
    isDone = objc_property()

    @objc_method
    def dealloc(self) -> None:
        # cleanup code here
        self.origLabelTxts = None
        self.seed = None
        self.seedList = None
        self.sugButs = None
        self.isDone = None
        send_super(__class__, self, 'dealloc')

    @objc_method
    def viewDidLoad(self) -> None:
        send_super(__class__, self, 'viewDidLoad')
        self.sugButs = list()
        utils.uilabel_replace_attributed_text(self.seedtv, " ", font = UIFont.systemFontOfSize_weight_(16.0, UIFontWeightBold))
        self.seedtv.text = '' # now clear it again..
        if not self.kvc:
            vcs = self.childViewControllers
            for vc in vcs:
                if isinstance(vc, KeyboardVC):
                    self.kvc = vc
        if self.kvc:
            self.kvc.textInput = self.seedtv
            def callback() -> None: self.doSuggestions()
            self.kvc.textChanged = Block(callback)
            self.kvc.lowerCaseInsert = True
        else:
            utils.NSLog("ERROR: NewWalletSeed2 cannot find the KeyboardVC! FIXME!")
              
    @objc_method
    def translateUI(self) -> None:
        lbls = [ self.seedTit, self.info ]
        if not self.origLabelTxts:
            self.origLabelTxts = { lbl.ptr.value : lbl.text for lbl in lbls }
        d = self.origLabelTxts
        for lbl in lbls:
            if lbl.ptr == self.info.ptr:
                txt = _('Your seed is important!') + ' ' + _('To make sure that you have properly saved your seed, please retype it here.') + ' ' + _('Use the quick suggestions to save time.')
                #utils.uilabel_replace_attributed_text(lbl, _(d[lbl.ptr.value]), font=UIFont.italicSystemFontOfSize_(14.0))
                utils.uilabel_replace_attributed_text(lbl, txt, font=UIFont.italicSystemFontOfSize_(14.0))
            else:
                lbl.setText_withKerning_(_(d[lbl.ptr.value]), utils._kern)
   
    @objc_method
    def viewWillAppear_(self, animated : bool) -> None:
        send_super(__class__, self, 'viewWillAppear:', animated, argtypes=[c_bool])
        self.translateUI()
        self.doSuggestions()
 
    @objc_method
    def viewDidAppear_(self, animated : bool) -> None:
        send_super(__class__, self, 'viewDidAppear:', animated, argtypes=[c_bool])
        #print("got seed list",*self.seedList)
        self.seedtv.becomeFirstResponder()
 
    @objc_method
    def doSuggestions(self) -> None:
        t = str(self.seedtv.text).lower()
        prefix = ''
        words = t.split()
        wordNum = len(words)  
        if t and t[-1] != ' ':
            wordNum = wordNum - 1
            prefix = words[-1]
        
        suggestions = list(_Mnem().get_suggestions(prefix))
        #print("wordnum=",wordNum,"prefix=","'"+prefix+"'","suggestions:",*suggestions)
        
        self.kvc.disableAllKeys()
        self.kvc.setKey_enabled_(self.kvc.backspace, True)
        validchars = set()
        for sug in suggestions:
            l = len(prefix)
            if len(sug) > l:
                validchars.add(sug[l].upper())
        for c in validchars:
            self.kvc.setKey_enabled_(c, True)
            
        # next, do suggestion buttons
        sugButs = py_from_ns(self.sugButs)
        for but in sugButs:
            but.removeFromSuperview()
        sugButs = list()
        self.sugButs = list()
        
        currActualSeedWord = ''
        try:
            currActualSeedWord = self.seedList[wordNum]
        except:
            utils.NSLog("Error with seed word: %s",sys.exc_info()[1])
            currActualSeedWord = 'TOO MANY WORDS!' # this makes sure we continue even though they have too many words.
        
        #print("currActualSeedWord=",currActualSeedWord)
        
        if len(suggestions) < 10:
            import random
            sugSet = set()
            if currActualSeedWord in suggestions:
                sugSet.add(currActualSeedWord)
            while len(sugSet) < len(suggestions) and len(sugSet) < 4:
                sugSet.add(suggestions[random.randint(0,len(suggestions)-1)])
            #print("sugSet=",*sugSet if sugSet else '')
            for sug in sugSet:
                def AddButWord(but : objc_id) -> None:
                    but = ObjCInstance(but)
                    word = but.titleForState_(UIControlStateNormal)
                    try:
                        self.seedtv.setText_((' '.join(words[:wordNum]) + (' ' if wordNum else '') + word + ' ').lower())
                    except:
                        utils.NSLog("Could not set textView: %s",sys.exc_info()[1])
                    self.doSuggestions()
                but = SuggestionButton.suggestionButtonWithText_handler_(sug, AddButWord)
                sugButs.append(but)
                
            # lay out buttons
            nButs = len(sugButs)
            if nButs:
                marg = 15.0
                pad = 5.0
                kvcY = self.kvcContainerView.frame.origin.y
                fw = self.view.frame.size.width
                insetWidth = fw - marg*2.0
                totalPad = pad * (nButs-1)
                w = min( (insetWidth - totalPad)/nButs, 200.0 )
                posX = (fw - (w*nButs + totalPad))/2.0 
                for but in sugButs:
                    f = but.frame
                    f.size.width = w
                    f.origin.x = posX
                    posX += w + pad
                    f.origin.y = kvcY - f.size.height - marg
                    but.frame = f
                    self.view.addSubview_(but)

        self.sugButs = sugButs
        
        self.errMsgView.setHidden_(True)
        self.infoView.setHidden_(False)

    @objc_method
    def viewWillTransitionToSize_withTransitionCoordinator_(self, size : CGSize, coordinator : ObjCInstance) -> None:
        send_super(__class__, self, 'viewWillTransitionToSize:withTransitionCoordinator:', size, coordinator, argtypes=[CGSize,objc_id])
        # hack to handle rotaton correctly by laying out the buttons all over again
        def layoutButtons() -> None:
            self.doSuggestions()
            self.autorelease()
        if list(self.sugButs):
            self.retain()
            utils.call_later(0.400, layoutButtons)
    
    @objc_method
    def shouldPerformSegueWithIdentifier_sender_(self, identifier, sender) -> bool:
        if identifier == 'EMBEDDED_KVC': return True
        return False
        
    @objc_method
    def onNext(self) -> None:
        def doDismiss() -> None:
            self.presentingViewController.dismissViewControllerAnimated_completion_(True, None)

        if self.isDone:
            doDismiss()
            return
            
        parent = gui.ElectrumGui.gui
        
        if list(self.seedList) != self.seedtv.text.strip().lower().split():
            err = _('The seed you entered does not match the generated seed. Go back to the previous screen and double-check it, then try again.')
            utils.uilabel_replace_attributed_text(self.errMsg, err, font=UIFont.italicSystemFontOfSize_(14.0))
            self.errMsgView.setHidden_(False)
            self.infoView.setHidden_(True)
            return
        
        try:
            wallet_name = _Params(self)['WalletName']
            wallet_pass = _Params(self)['WalletPass']
            wallet_seed = _Params(self)['seed']
        except:
            utils.NSLog("onNext in Seed2, got exception: %s", str(sys.exc_info()[1]))
            return

        def openNew() -> None:
            parent.switch_wallets(wallet_name = wallet_name, wallet_pass = wallet_pass, vc = self, onSuccess=doDismiss,
                                  onFailure=onFailure, onCancel=doDismiss)
        def onSuccess() -> None:
            self.isDone = True
            parent.refresh_components('wallets')
            parent.show_message(vc=self, title=_('New Wallet Created'),
                                message = _('Your new standard wallet has been successfully created. Would you like to switch to it now?'),
                                hasCancel = True, cancelButTitle = _('No'), okButTitle=_('Open New Wallet'),
                                onOk = openNew, onCancel = doDismiss)
        def onFailure(err : str) -> None:
            parent.show_error(vc=self, message = str(err))
            
        parent.generate_new_standard_wallet(
            vc = self,
            wallet_name = wallet_name,
            wallet_pass = wallet_pass,
            wallet_seed = wallet_seed,
            onSuccess = onSuccess,
            onFailure = onFailure)
 
    
    @objc_method
    def prepareForSegue_sender_(self, segue, sender) -> None:
        # TODO: stuff
        print("params=",_Params(self))


class NewWalletMenu(NewWalletMenuBase):
    lineHider = objc_property()
    noCancelBut = objc_property()
    
    @objc_method
    def dealloc(self) -> None:
        self.lineHider = None
        self.noCancelBut = None
        send_super(__class__, self, 'dealloc')
        
    @objc_method
    def viewDidLoad(self) -> None:
        send_super(__class__, self, 'viewDidLoad')

    @objc_method
    def viewWillAppear_(self, animated : bool) -> None:
        send_super(__class__, self, 'viewWillAppear:', animated, argtypes=[c_bool])
        if self.noCancelBut:
            self.navigationItem.leftBarButtonItem = None
            self.noCancelBut = None
        navBar = self.navigationController.navigationBar if self.navigationController else None
        if navBar:
            f = navBar.frame
            # This line hider is a hack/fix for a weirdness in iOS where there is a white line between the top nav bar and the bottom
            # main area.  This hopefully fixes that.
            self.lineHider = UIView.alloc().initWithFrame_(CGRectMake(0,f.size.height,f.size.width,1)).autorelease()
            self.lineHider.backgroundColor = navBar.barTintColor
            self.lineHider.autoresizingMask = (1<<6)-1
            navBar.addSubview_(self.lineHider)

    @objc_method
    def viewWillDisappear_(self, animated : bool) -> None:
        send_super(__class__, self, 'viewWillDisappear:', animated, argtypes=[c_bool])
        if self.lineHider:
            self.lineHider.removeFromSuperview()
            self.lineHider = None
    
    @objc_method
    def unimplemented(self) -> None:
        gui.ElectrumGui.gui.show_error(title="Unimplemented",message="Coming Soon!", vc = self)

def _Params(vc : UIViewController) -> dict():
    return py_from_ns(vc.navigationController.params)

def _SetParams(vc : UIViewController, params : dict) -> None:
    vc.navigationController.params = params

def _SetParam(vc : UIViewController, paramName : str, paramValue : Any) -> None:
    d = _Params(vc)
    if not paramValue:
        d.pop(paramName, None)
    else:
        d[paramName] = paramValue
    _SetParams(vc, d)

_mnem = None   
def _Mnem() -> None:
    global _mnem
    if not _mnem: _mnem = Mnemonic()
    return _mnem

def _lowMemory(notificaton : objc_id) -> None:
    # low memory warning -- loop through cache and release all cached images
    ct = 0
    global _mnem
    if _mnem:
        _mnem = None
        ct += 1
    if ct:
        import os
        utils.NSLog("Low Memory: Flushed %d objects from %s static globals"%(ct,os.path.split(str(__file__))[-1]))

_notification_token = NSNotificationCenter.defaultCenter.addObserverForName_object_queue_usingBlock_(
    UIApplicationDidReceiveMemoryWarningNotification,
    UIApplication.sharedApplication,
    None,
    Block(_lowMemory)
).retain()


#####
# On-Boarding Wizard that comes up on first run when no wallets are present
#####
def PresentOnBoardingWizard(vc : ObjCInstance = None, animated : bool = True, completion : Block = None, dontPresentJustReturnIt = False) -> ObjCInstance:
    if not vc: vc = gui.ElectrumGui.gui.get_presented_viewcontroller()
    sb = UIStoryboard.storyboardWithName_bundle_("NewWallet", None)
    if not sb:
        utils.NSLog("ERROR: SB IS NULL")
        return None
    wiz = sb.instantiateViewControllerWithIdentifier_("On_Boarding")
    if wiz:
        if not dontPresentJustReturnIt:
            vc.presentViewController_animated_completion_(wiz, animated, completion)
    else:
        utils.NSLog("ERROR: Could not find the storyboard viewcontroller named 'On_Boarding'!")
    return wiz

class OnBoardingWizard(OnBoardingWizardBase):
    ''' On-Boarding Wizard that comes up on first run when no wallets are present'''
    pvc = objc_property()
    vcs = objc_property()


    @objc_method
    def dealloc(self) -> None:
        self.pvc = None
        self.vcs = None
        send_super(__class__, self, 'dealloc')
        
    @objc_method
    def viewDidLoad(self) -> None:
        send_super(__class__, self, 'viewDidLoad')
        vcs = self.childViewControllers
        for vc in vcs:
            if isinstance(vc, UIPageViewController):
                self.pvc = vc
        if self.pvc:
            self.pvc.dataSource = self
            sb = UIStoryboard.storyboardWithName_bundle_("NewWallet", None)
            if not sb:
                utils.NSLog("ERROR: SB IS NULL")
                return
            vcs = [ sb.instantiateViewControllerWithIdentifier_("On_Boarding_%d" % i) for i in range(1,4) ]
            vcs.append( sb.instantiateViewControllerWithIdentifier_("On_Boarding_Menu") )
            if not vcs or None in vcs:
                utils.NSLog("ERROR: Could not find a requisite viewcontroller in %s viewDidLoag method!",str(__class__))
                return
            self.vcs = ns_from_py(vcs)
            self.pvc.setViewControllers_direction_animated_completion_(NSArray.arrayWithObject_(vcs[0]),UIPageViewControllerNavigationDirectionForward,False,None)
        else:
            utils.NSLog("ERROR: Could not find the UIPageViewController in the %s viewDidLoad method!",str(__class__))

    @objc_method
    def preferredStatusBarStyle(self) -> int:
        return UIStatusBarStyleLightContent
    
    @objc_method
    def presentationCountForPageViewController_(self, pvc) -> int:
        return len(self.vcs) if self.vcs else 0

    @objc_method
    def presentationIndexForPageViewController_(self, pvc) -> int:
        return 0
    
    @objc_method
    def pageViewController_viewControllerBeforeViewController_(self, pvc, vc) -> ObjCInstance:
        b4 = None
        vcs = py_from_ns(self.vcs)
        for i,v in enumerate(vcs):
            if v == vc and i > 0:
                b4 = vcs[i-1]
        return b4
            
    
    @objc_method
    def pageViewController_viewControllerAfterViewController_(self, pvc, vc) -> ObjCInstance:
        aft = None
        vcs = py_from_ns(self.vcs)
        for i,v in enumerate(vcs):
            if v == vc and i+1 < len(vcs):
                aft = vcs[i+1]
        return aft

class OnBoardingMenu(NewWalletMenuBase):
    @objc_method
    def viewDidLoad(self) -> None:
        send_super(__class__, self, 'viewDidLoad')
        pass
    @objc_method
    def unimplemented(self) -> None:
        gui.ElectrumGui.gui.show_error(title="Unimplemented",message="Coming Soon!", vc = self)

    @objc_method
    def onNewStandardWallet(self) -> None:
        vc = PresentAddWalletWizard(dontPresentJustReturnIt = True)
        # hacky mechanism to get to the second viewcontroller in this storyboard.. it works but isn't 100% pretty
        if isinstance(vc, UINavigationController) and vc.viewControllers and isinstance(vc.viewControllers[0], NewWalletMenu):
            menu = vc.viewControllers[0]
            menu.noCancelBut = True
            _SetParam(menu, 'is_onboarding_wizard', True)
            sb = UIStoryboard.storyboardWithName_bundle_('NewWallet', None)
            if sb:
                vc2 = sb.instantiateViewControllerWithIdentifier_("NewStandardWallet") #NB: If you rename it in storyboard be SURE to update this!
                if vc2:
                    vc.pushViewController_animated_(vc2, False)
                    pvc = self.presentingViewController
                    if pvc:
                        pvc.dismissViewControllerAnimated_completion_(True, None)
                        pvc.presentViewController_animated_completion_(vc, True, None)
                        return
        # If this is reached, means the above failed
        gui.ElectrumGui.gui.show_error(title = "Oops!", message = "Something went wrong! Please email the developers!")
