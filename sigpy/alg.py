import numpy as np
from tqdm import tqdm
from sigpy import util, config

if config.cupy_enabled:
    import cupy as cp


class Alg(object):
    """Abstraction for iterative algorithm.

    Args:
        max_iter (int): Maximum number of iterations.
        device (int or Device): Device.

    """
    def __init__(self, max_iter, device, progress_bar=True):
        self.max_iter = max_iter
        self.device = util.Device(device)
        self.progress_bar = progress_bar

    def _init(self):
        return

    def _update(self):
        raise NotImplementedError

    def _done(self):
        return self.iter >= self.max_iter

    def _cleanup(self):
        return

    def init(self):
        if self.progress_bar:
            self.pbar = tqdm(total=self.max_iter, desc=self.__class__.__name__)
            
        self.iter = 0
        with self.device:
            self._init()

    def update(self):
        with self.device:
            self._update()

            self.iter += 1
            if self.progress_bar:
                self.pbar.update()

    def done(self):
        with self.device:
            return self._done()

    def cleanup(self):
        if self.progress_bar:
            self.pbar.close()
            
        self._cleanup()


class PowerMethod(Alg):
    """Power method to estimate maximum eigenvalue and eigenvector.

    Args:
        A (function): Function to a hermitian linear mapping.
        x (array): Variable to optimize over.
        max_iter (int): Maximum number of iterations.

    Attributes:
        float: Maximum eigenvalue of `A`.

    """
    def __init__(self, A, x, max_iter=30, progress_bar=True):
        self.A = A
        self.x = x

        super().__init__(max_iter, util.get_device(x), progress_bar=progress_bar)

    def _init(self):
        xp = util.get_xp(self.x)
        self.max_eig = xp.array(np.infty)

    def _update(self):
        y = self.A(self.x)
        self.max_eig = util.norm(y)
        util.move_to(self.x, y / self.max_eig)
        if self.progress_bar:
            self.pbar.set_postfix(max_eig=self.max_eig)


class ProximalPointMethod(Alg):
    """Proximal point method.

    """
    def __init__(self, proxf, alpha, x, max_iter=100, device=util.cpu_device, progress_bar=True):
        self.proxf = proxf
        self.alpha = alpha
        self.x = x
        
        super().__init__(max_iter, device=device, progress_bar=progress_bar)

    def _update(self):
        util.move_to(self.x, self.proxf(self.alpha, self.x))


class GradientMethod(Alg):
    """First order gradient method.

    Considers the composite cost function:

    .. math:: f(x) + g(x)

    where f is smooth, and g is simple,
    ie proximal operator of g is simple to compute.

    Args:
        gradf (function): function to compute gradient of f.
        x (array): variable to optimize over.
        alpha (float): step size.
        proxg (function or None): function to compute proximal mapping of g.
        accelerate (bool): toggle Nesterov acceleration.
        P (function or None): function to precondition, assumes proxg has already incorporated P.
        max_iter (int): maximum number of iterations.

    References:
        Nesterov, Y. E. (1983). 
        A method for solving the convex programming problem with convergence rate 
        O (1/k^ 2). In Dokl. Akad. Nauk SSSR (Vol. 269, pp. 543-547).

        Beck, A., & Teboulle, M. (2009). 
        A fast iterative shrinkage-thresholding algorithm for linear inverse problems. 
        SIAM journal on imaging sciences, 2(1), 183-202.

    """
    def __init__(self, gradf, x, alpha, proxg=None,
                 accelerate=False, max_iter=100, progress_bar=True):
        self.gradf = gradf
        self.alpha = alpha
        self.accelerate = accelerate
        self.proxg = proxg
        self.x = x

        super().__init__(max_iter, util.get_device(x), progress_bar=progress_bar)

    def _init(self):
        if self.accelerate:
            self.z = self.x.copy()
            self.t = 1

        if self.accelerate or self.proxg is not None:
            self.x_old = self.x.copy()

        self.residual = np.infty

    def _update(self):
        if self.accelerate or self.proxg is not None:
            util.move_to(self.x_old, self.x)

        if self.accelerate:
            util.move_to(self.x, self.z)

        gradf_x = self.gradf(self.x)
            
        util.axpy(self.x, -self.alpha, gradf_x)

        if self.proxg is not None:
            util.move_to(self.x, self.proxg(self.alpha, self.x))

        if self.accelerate:
            t_old = self.t
            self.t = (1 + (1 + 4 * t_old**2)**0.5) / 2
            util.move_to(self.z, self.x + (t_old - 1) / self.t * (self.x - self.x_old))

        if self.accelerate or self.proxg is not None:
            self.residual = util.move(util.norm((self.x - self.x_old) / self.alpha**0.5))
        else:
            self.residual = util.move(util.norm(gradf_x))
            
        if self.progress_bar:
            self.pbar.set_postfix(resid=self.residual)

    def _done(self):
        return (self.iter >= self.max_iter) or self.residual == 0

    def _cleanup(self):
        if self.accelerate:
            del self.z
            del self.t

        if self.accelerate or self.proxg is not None:
            del self.x_old


class ConjugateGradient(Alg):
    r"""Conjugate Gradient Method. Solves for:

    .. math:: A x = b
    where A is hermitian.

    Args:
        A (function): A hermitian linear function.
        b (array): Observation.
        x (array): Variable.
        P (function or None): Preconditioner.
        max_iter (int): Maximum number of iterations.

    """
    def __init__(self, A, b, x, P=None, max_iter=100, progress_bar=True):
        self.A = A
        self.P = P
        self.x = x
        self.b = b
        self.rzold = np.infty

        super().__init__(max_iter, util.get_device(x), progress_bar=progress_bar)

    def _init(self):
        self.b -= self.A(self.x)
        self.r = self.b
        if self.P is None:
            z = self.r
        else:
            z = self.P(self.r)
            
        if self.max_iter > 1:
            self.p = z.copy()
        else:
            self.p = z

        self.zero_gradient = False

        self.rzold = util.dot(self.r, z)
        self.residual = util.move(self.rzold**0.5)

    def _update(self):
        Ap = self.A(self.p)
        pAp = util.dot(self.p, Ap)

        if pAp == 0:
            self.zero_gradient = True
            return

        self.alpha = self.rzold / pAp
        util.axpy(self.x, self.alpha, self.p)

        if self.iter < self.max_iter - 1:
            util.axpy(self.r, -self.alpha, Ap)

            if self.P is not None:
                z = self.P(self.r)
            else:
                z = self.r
                
            rznew = util.dot(self.r, z)
            beta = rznew / self.rzold

            util.xpay(self.p, beta, z)

            self.rzold = rznew

        self.residual = util.move(self.rzold**0.5)
        
        if self.progress_bar:
            self.pbar.set_postfix(resid=self.residual)

    def _done(self):
        return (self.iter >= self.max_iter) or self.zero_gradient or self.residual == 0

    def _cleanup(self):
        del self.r
        del self.p
        del self.rzold


class NewtonsMethod(Alg):
    r"""Newton's Method with composite self-concordant formulation.

    Considers the objective function:
    
    .. math:: f(x) + g(x),
    where f is smooth and g is simple.

    Args:
        gradf (function): Function to compute gradient of f.
        hessf (function): Function to compute Hessian of f at x,
        proxHg (function): Function to compute proximal operator of g.
        x (array): Optimization variable.

    References:
        Tran-Dinh, Q., Kyrillidis, A., & Cevher, V. (2015). 
        Composite self-concordant minimization. 
        The Journal of Machine Learning Research, 16(1), 371-416.

    """
    def __init__(self, gradf, hessf, proxHg, x,
                 max_iter=10, sigma=(3 - 5**0.5) / 2, progress_bar=True):

        self.gradf = gradf
        self.hessf = hessf
        self.proxHg = proxHg
        self.sigma = sigma
        self.x = x
        self.lamda = np.infty

        super().__init__(max_iter, util.get_device(x), progress_bar=progress_bar)

    def _update(self):

        hessfx = self.hessf(self.x)

        s = self.proxHg(hessfx, hessfx(self.x) - self.gradf(self.x))

        d = s - self.x

        self.lamda = util.dot(d, hessfx(d))**0.5
        
        if self.progress_bar:
            self.pbar.set_postfix(lamda=self.lamda)

        self.x += alpha * d


class PrimalDualHybridGradient(Alg):
    r"""Primal dual hybrid gradient.

    Considers the problem:

    .. math:: \min_x \max_y g(x) - f^*(u) + <Ax, u>

    Or equivalently:

    .. math:: \min_x f(A x) + g(x)

    Args:
        proxfc (function): Function to compute proximal operator of f^*.
        proxg (function): Function to compute proximal operator of g.
        A (function): Function to compute a linear mapping.
        AH (function): Function to compute the adjoint linear mapping of `A`.
        x (array): Primal solution.
        u (array): Dual solution.
        tau (float or array): Primal step-size.
        sigma (float or array): Dual step-size.
        gamma_primal (float): Strong convexity parameter of g.
        gamma_dual (float): Strong convexity parameter of f^*.
        P (function): Function to compute precondition primal variable.
        D (function): Function to compute precondition dual variable.
        max_iter (int): Maximum number of iterations.

    References:
       Chambolle, A., & Pock, T. (2011).
       A first-order primal-dual algorithm for convex problems with 
       applications to imaging. Journal of mathematical imaging and vision, 40(1), 120-145.

    """
    def __init__(
            self, proxfc, proxg, A, AH, x, u,
            tau, sigma, gamma_primal=0, gamma_dual=0,
            P=lambda x: x, D=lambda x: x, max_iter=100, progress_bar=True
    ):

        self.proxfc = proxfc
        self.proxg = proxg

        self.A = A
        self.AH = AH

        self.u = u
        self.x = x

        self.tau = tau
        self.sigma = sigma
        self.gamma_primal = gamma_primal
        self.gamma_dual = gamma_dual

        super().__init__(max_iter, util.get_device(x), progress_bar=progress_bar)

    def _init(self):
        self.x_ext = self.x.copy()
        self.u_old = self.u.copy()
        self.x_old = self.x.copy()
        super()._init()

    def _update(self):
        util.move_to(self.u_old, self.u)
        util.move_to(self.x_old, self.x)

        util.move_to(self.u, self.proxfc(self.sigma, self.u + self.sigma * self.A(self.x_ext)))
        util.move_to(self.x, self.proxg(self.tau, self.x - self.tau * self.AH(self.u)))

        xp = self.device.xp
        if self.gamma_primal > 0 and self.gamma_dual == 0:
            theta = 1 / (1 + 2 * self.gamma_primal * xp.amin(xp.abs(self.tau)))**0.5
            self.tau *= theta
            self.sigma /= theta
        elif self.gamma_primal == 0 and self.gamma_dual > 0:
            theta = 1 / (1 + 2 * self.gamma_dual * xp.amin(xp.abs(self.sigma)))**0.5
            self.tau /= theta
            self.sigma *= theta
        else:
            theta = 1

        util.move_to(self.x_ext, self.x + theta * (self.x - self.x_old))

    def _cleanup(self):
        del self.x_ext
        del self.u_old
        del self.x_old


class AltMin(Alg):
    """Alternating Minimization.

    Args:
        min1 (function): Function to minimize over variable 1.
        min2 (function): Funciton to minimize over variable 2.
        max_iter (int): Maximum number of iterations.

    """

    def __init__(self, min1, min2, max_iter=30, progress_bar=True):
        self.min1 = min1
        self.min2 = min2

        super().__init__(max_iter, util.cpu_device, progress_bar=progress_bar)

    def _update(self):

        self.min1()
        self.min2()
